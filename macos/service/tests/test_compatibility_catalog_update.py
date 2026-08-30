from __future__ import annotations

import asyncio
import base64
import copy
from dataclasses import asdict
import fcntl
import json
import os
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mnemosyne_macos.compatibility_catalog import (
    MAX_CATALOG_BYTES,
    CatalogStore,
    CatalogVerifier,
    TrustedCatalogKey,
    canonical_json,
    catalog_digest,
    signing_message,
)
from mnemosyne_macos.compatibility_catalog_update import CatalogUpdateClient


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
PROTOCOL_V1 = REPOSITORY_ROOT / "compatibility_catalog" / "v1"
TEST_NOW = 1_790_000_000
TEST_SEED = bytes(range(1, 33))
ORIGIN = "https://catalog.mnemosyne.test"
PATH = "/v1/apple-silicon/catalog.json"


def _golden() -> dict:
    return json.loads(
        (PROTOCOL_V1 / "catalog.golden.json").read_text(encoding="utf-8")
    )


def _private() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(TEST_SEED)


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _resign(value: dict) -> dict:
    candidate = copy.deepcopy(value)
    candidate["catalog_digest"] = catalog_digest(candidate["catalog"])
    candidate["signatures"] = [
        {
            "key_id": "test-catalog-2026-a",
            "algorithm": "Ed25519",
            "signature": _encode(
                _private().sign(signing_message(candidate["catalog"]))
            ),
        }
    ]
    return candidate


def _at_sequence(value: dict, sequence: int) -> dict:
    candidate = copy.deepcopy(value)
    candidate["catalog"]["catalog_sequence"] = sequence
    candidate["catalog"]["catalog_version"] = f"test-{sequence}"
    return _resign(candidate)


def _verifier() -> CatalogVerifier:
    public = _private().public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    key = TrustedCatalogKey(
        key_id="test-catalog-2026-a",
        public_key=public,
    )
    return CatalogVerifier({key.key_id: key})


def _store(tmp_path: Path) -> CatalogStore:
    return CatalogStore(tmp_path / "catalog-state", _verifier())


def _response(
    status: int,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    merged = dict(headers or {})
    if body is not None and "Content-Type" not in merged:
        merged["Content-Type"] = "application/json"
    return httpx.Response(
        status,
        stream=httpx.ByteStream(body or b""),
        headers=merged,
    )


def _client(
    tmp_path: Path,
    handler,
    *,
    max_attempts: int = 1,
    total_timeout_seconds: float = 1.0,
) -> tuple[CatalogUpdateClient, CatalogStore]:
    store = _store(tmp_path)
    client = CatalogUpdateClient(
        store=store,
        origin=ORIGIN,
        path=PATH,
        max_attempts=max_attempts,
        total_timeout_seconds=total_timeout_seconds,
        connect_timeout_seconds=min(0.5, total_timeout_seconds),
        transport=httpx.MockTransport(handler),
        clock=lambda: TEST_NOW,
    )
    return client, store


@pytest.mark.asyncio
async def test_success_is_canonical_credential_free_and_atomically_activated(
    tmp_path: Path,
) -> None:
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.method == "GET"
        assert str(request.url) == ORIGIN + PATH
        assert await request.aread() == b""
        assert request.headers["accept"] == "application/json"
        assert request.headers["accept-encoding"] == "identity"
        assert request.headers["user-agent"] == "Mnemosyne-Catalog-Updater/1"
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        return _response(
            200,
            body=canonical_json(_golden()),
            headers={"ETag": '"catalog-42"'},
        )

    client, store = _client(tmp_path, handler)
    assert client.status().last_outcome == "never"
    result = await client.check()
    assert result.outcome == "updated"
    assert result.changed is True
    assert result.error_code is None
    assert result.catalog_sequence == 42
    assert result.catalog_source == "signed"
    assert client.status().state == "idle"
    assert client.status().conditional_request_ready is True
    assert store.load(now=TEST_NOW).catalog_sequence == 42
    assert len(seen) == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_etag_no_change_is_bounded_private_and_requires_a_prior_success(
    tmp_path: Path,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            assert "if-none-match" not in request.headers
            return _response(
                200,
                body=canonical_json(_golden()),
                headers={"ETag": 'W/"catalog-42"'},
            )
        assert request.headers["if-none-match"] == 'W/"catalog-42"'
        return _response(304, headers={"ETag": '"catalog-42-final"'})

    client, _store_value = _client(tmp_path, handler)
    assert (await client.check()).outcome == "updated"
    result = await client.check()
    assert result.outcome == "not_modified"
    assert result.changed is False
    assert result.catalog_sequence == 42
    status_text = json.dumps(asdict(client.status()), sort_keys=True)
    assert "catalog-42" not in status_text
    assert ORIGIN not in status_text
    assert PATH not in status_text
    assert calls == 2
    await client.aclose()

    invalid, _ = _client(
        tmp_path / "fresh",
        lambda _request: _response(304),
    )
    rejected = await invalid.check()
    assert rejected.error_code == "catalog_update_response_invalid"
    await invalid.aclose()


@pytest.mark.parametrize(
    ("origin", "path"),
    [
        ("http://catalog.mnemosyne.test", PATH),
        ("https://Catalog.mnemosyne.test", PATH),
        ("https://user@catalog.mnemosyne.test", PATH),
        ("https://catalog.mnemosyne.test:0", PATH),
        ("https://catalog.mnemosyne.test:443", PATH),
        ("https://catalog.mnemosyne.test/base", PATH),
        (ORIGIN, "v1/catalog.json"),
        (ORIGIN, "/v1/../catalog.json"),
        (ORIGIN, "/v1/%63atalog.json"),
        (ORIGIN, "/v1/catalog.json?secret=true"),
    ],
)
def test_only_a_canonical_verified_https_origin_and_path_are_accepted(
    tmp_path: Path,
    origin: str,
    path: str,
) -> None:
    with pytest.raises(ValueError, match="catalog_update_endpoint_invalid"):
        CatalogUpdateClient(
            store=_store(tmp_path),
            origin=origin,
            path=path,
            transport=httpx.MockTransport(
                lambda _request: _response(500)
            ),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "code"),
    [
        (
            _response(302, headers={"Location": "https://elsewhere.test/x"}),
            "catalog_update_redirect_rejected",
        ),
        (
            httpx.Response(
                200,
                stream=httpx.ByteStream(b"compressed-secret"),
                headers={
                    "Content-Type": "application/json",
                    "Content-Encoding": "gzip",
                },
            ),
            "catalog_update_encoding_rejected",
        ),
        (
            _response(
                200,
                body=b"{}",
                headers={"Content-Type": "text/plain"},
            ),
            "catalog_update_media_type_rejected",
        ),
        (
            _response(
                200,
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": str(MAX_CATALOG_BYTES + 1),
                },
            ),
            "catalog_update_response_too_large",
        ),
    ],
)
async def test_redirect_compression_media_type_and_oversize_fail_closed(
    tmp_path: Path,
    response: httpx.Response,
    code: str,
) -> None:
    client, _ = _client(tmp_path, lambda _request: response)
    result = await client.check()
    assert result.outcome == "failed"
    assert result.error_code == code
    assert result.catalog_source == "built_in"
    await client.aclose()


@pytest.mark.asyncio
async def test_streaming_body_limit_is_enforced_without_activation(
    tmp_path: Path,
) -> None:
    body = b"x" * (MAX_CATALOG_BYTES + 1)
    client, store = _client(
        tmp_path,
        lambda _request: _response(200, body=body),
    )
    result = await client.check()
    assert result.error_code == "catalog_update_response_too_large"
    assert store.load(now=TEST_NOW).source == "built_in"
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 403, 500])
async def test_http_auth_and_error_bodies_are_never_returned_or_recorded(
    tmp_path: Path,
    status_code: int,
) -> None:
    secret = "secret-body-and-endpoint.example"
    client, _ = _client(
        tmp_path,
        lambda _request: httpx.Response(status_code, text=secret),
    )
    result = await client.check()
    assert result.error_code == "catalog_update_http_error"
    public = json.dumps(asdict(result), sort_keys=True) + json.dumps(
        asdict(client.status()), sort_keys=True
    )
    assert secret not in public
    assert ORIGIN not in public
    assert PATH not in public
    await client.aclose()


@pytest.mark.asyncio
async def test_invalid_signature_and_same_sequence_conflict_are_rejected(
    tmp_path: Path,
) -> None:
    responses: list[bytes] = []
    baseline = _at_sequence(_golden(), 50)
    responses.append(canonical_json(baseline))
    conflict = copy.deepcopy(baseline)
    conflict["catalog"]["catalog_version"] = "different-sequence-50"
    responses.append(canonical_json(_resign(conflict)))
    tampered = _at_sequence(_golden(), 51)
    tampered["catalog"]["catalog_version"] = "tampered-after-signing"
    responses.append(canonical_json(tampered))

    client, store = _client(
        tmp_path,
        lambda _request: _response(200, body=responses.pop(0)),
    )
    assert (await client.check()).outcome == "updated"
    active = tmp_path / "catalog-state" / "active.catalog.json"
    before = active.read_bytes()
    conflict_result = await client.check()
    assert conflict_result.error_code == "catalog_update_version_conflict"
    assert active.read_bytes() == before
    signature_result = await client.check()
    assert signature_result.error_code == "catalog_update_catalog_untrusted"
    assert active.read_bytes() == before
    assert store.load(now=TEST_NOW).catalog_sequence == 50
    await client.aclose()


@pytest.mark.asyncio
async def test_transport_timeout_retries_within_one_total_deadline(
    tmp_path: Path,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("private diagnostic", request=request)
        return _response(200, body=canonical_json(_golden()))

    client, _ = _client(tmp_path, handler, max_attempts=2)
    result = await client.check()
    assert result.outcome == "updated"
    assert calls == 2
    assert "private diagnostic" not in repr(result)
    await client.aclose()


@pytest.mark.asyncio
async def test_concurrent_checks_share_exactly_one_fetch(tmp_path: Path) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return _response(200, body=canonical_json(_golden()))

    client, _ = _client(tmp_path, handler)
    first = asyncio.create_task(client.check())
    await entered.wait()
    second = asyncio.create_task(client.check())
    await asyncio.sleep(0)
    assert client.status().state == "checking"
    release.set()
    first_result, second_result = await asyncio.gather(first, second)
    assert first_result == second_result
    assert calls == 1
    assert client.status().state == "idle"
    await client.aclose()


@pytest.mark.asyncio
async def test_waiter_and_close_cancellation_do_not_leak_the_shared_client(
    tmp_path: Path,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return _response(200, body=canonical_json(_golden()))

    client, store = _client(tmp_path, handler)
    waiter = asyncio.create_task(client.check())
    await entered.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    closing = asyncio.create_task(client.aclose())
    await asyncio.sleep(0)
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    release.set()
    await client.aclose()
    assert calls == 1
    assert client.status().state == "closed"
    assert store.load(now=TEST_NOW).catalog_sequence == 42
    closed = await client.check()
    assert closed.error_code == "catalog_update_closed"
    closed_results = await asyncio.gather(*(client.check() for _ in range(5)))
    assert all(
        result.error_code == "catalog_update_closed"
        for result in closed_results
    )
    await asyncio.sleep(0)
    assert calls == 1
    assert client.status().state == "closed"


@pytest.mark.asyncio
async def test_invalid_clock_isolated_before_network_and_negative_fraction_rejected(
    tmp_path: Path,
) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _response(200, body=canonical_json(_golden()))

    store = _store(tmp_path)
    client = CatalogUpdateClient(
        store=store,
        origin=ORIGIN,
        path=PATH,
        max_attempts=1,
        transport=httpx.MockTransport(handler),
        clock=lambda: -0.5,
    )
    result = await client.check()
    assert result.error_code == "catalog_update_internal_error"
    assert result.checked_at == 0
    assert calls == 0
    assert store.load(now=TEST_NOW).source == "built_in"
    await client.aclose()


@pytest.mark.asyncio
async def test_store_contention_is_bounded_and_does_not_start_http(
    tmp_path: Path,
) -> None:
    calls = 0
    store = _store(tmp_path)
    store.activate(_golden(), now=TEST_NOW)
    active = tmp_path / "catalog-state" / "active.catalog.json"
    before = active.read_bytes()

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _response(200, body=canonical_json(_golden()))

    client = CatalogUpdateClient(
        store=store,
        origin=ORIGIN,
        path=PATH,
        max_attempts=1,
        transport=httpx.MockTransport(handler),
        clock=lambda: TEST_NOW,
    )
    descriptor = os.open(tmp_path / "catalog-state" / "catalog.lock", os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = await asyncio.wait_for(client.check(), timeout=0.5)
        assert result.error_code == "catalog_update_store_error"
        assert calls == 0
        assert active.read_bytes() == before
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    assert store.load(now=TEST_NOW).catalog_sequence == 42
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        ("network", "catalog_update_network_error"),
        ("http", "catalog_update_http_error"),
        ("parse", "catalog_update_catalog_invalid"),
        ("signature", "catalog_update_catalog_untrusted"),
        ("expired", "catalog_update_catalog_expired"),
        ("downgrade", "catalog_update_downgrade_rejected"),
    ],
)
async def test_every_failed_update_preserves_exact_last_known_good_bytes(
    tmp_path: Path,
    failure: str,
    expected: str,
) -> None:
    calls = 0
    baseline = _at_sequence(_golden(), 50)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _response(200, body=canonical_json(baseline))
        if failure == "network":
            raise httpx.ConnectError("private network detail", request=request)
        if failure == "http":
            return httpx.Response(503, text="private server body")
        if failure == "parse":
            return _response(200, body=b"{")
        if failure == "signature":
            candidate = _at_sequence(_golden(), 51)
            candidate["catalog"]["catalog_version"] = "tampered"
            return _response(200, body=canonical_json(candidate))
        if failure == "expired":
            candidate = _at_sequence(_golden(), 51)
            candidate["catalog"]["issued_at"] = TEST_NOW - 100
            candidate["catalog"]["expires_at"] = TEST_NOW
            return _response(200, body=canonical_json(_resign(candidate)))
        return _response(200, body=canonical_json(_golden()))

    client, store = _client(tmp_path, handler, max_attempts=1)
    assert (await client.check()).catalog_sequence == 50
    active = tmp_path / "catalog-state" / "active.catalog.json"
    before = active.read_bytes()
    rejected = await client.check()
    assert rejected.error_code == expected
    assert rejected.catalog_sequence == 50
    assert active.read_bytes() == before
    assert store.load(now=TEST_NOW).catalog_sequence == 50
    public = json.dumps(asdict(rejected), sort_keys=True) + json.dumps(
        asdict(client.status()), sort_keys=True
    )
    assert "private" not in public
    assert ORIGIN not in public
    assert PATH not in public
    await client.aclose()
