from __future__ import annotations

import ssl
from collections.abc import Iterable
from typing import Any

import httpcore
import httpx
import pytest

from mnemosyne_fleet.locator_policy import ResolvedLocator
from mnemosyne_fleet.paired_transport import (
    PairedTransportError,
    PinnedNodeTransport,
    create_pinned_node_client,
)


class _ScriptedStream(httpcore.AsyncNetworkStream):
    def __init__(
        self,
        response: bytes,
        *,
        peer: Any,
    ) -> None:
        self._reads = [response]
        self.peer = peer
        self.writes: list[bytes] = []
        self.tls_calls: list[tuple[ssl.SSLContext, str | None, float | None]] = []
        self.closed = False

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        del max_bytes, timeout
        return self._reads.pop(0) if self._reads else b""

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        del timeout
        self.writes.append(buffer)

    async def aclose(self) -> None:
        self.closed = True

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.AsyncNetworkStream:
        self.tls_calls.append((ssl_context, server_hostname, timeout))
        return self

    def get_extra_info(self, info: str) -> Any:
        if info == "server_addr":
            return self.peer
        if info == "ssl_object":
            return None
        if info == "is_readable":
            return False
        return None


class _ScriptedBackend(httpcore.AsyncNetworkBackend):
    def __init__(
        self,
        outcomes: dict[str, _ScriptedStream | Exception],
    ) -> None:
        self.outcomes = outcomes
        self.calls: list[tuple[str, int, float | None]] = []

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        del local_address, socket_options
        self.calls.append((host, port, timeout))
        outcome = self.outcomes[host]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        del path, timeout, socket_options
        raise AssertionError("paired transports must never use Unix sockets")

    async def sleep(self, seconds: float) -> None:
        del seconds


def _locator(
    *,
    origin: str = "https://worker.example.internal:1240",
    transport: str = "https",
    host: str = "worker.example.internal",
    port: int = 1240,
    addresses: tuple[str, ...] = ("10.20.30.40",),
) -> ResolvedLocator:
    return ResolvedLocator(
        origin=origin,
        transport=transport,  # type: ignore[arg-type]
        host=host,
        port=port,
        addresses=addresses,
    )


def _response(
    body: bytes = b"ok",
    *,
    status: int = 200,
    headers: tuple[tuple[bytes, bytes], ...] = (),
) -> bytes:
    reason = b"OK" if status == 200 else b"Found"
    rendered = [
        b"HTTP/1.1 " + str(status).encode("ascii") + b" " + reason + b"\r\n",
        b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n",
        b"Connection: close\r\n",
    ]
    rendered.extend(name + b": " + value + b"\r\n" for name, value in headers)
    rendered.extend((b"\r\n", body))
    return b"".join(rendered)


async def test_https_connects_only_to_pinned_addresses_and_keeps_origin_sni() -> None:
    locator = _locator(addresses=("10.20.30.39", "10.20.30.40"))
    stream = _ScriptedStream(
        _response(b'{"ok":true}'),
        peer=("10.20.30.40", 1240),
    )
    backend = _ScriptedBackend(
        {
            "10.20.30.39": httpcore.ConnectError("not available"),
            "10.20.30.40": stream,
        }
    )

    async with create_pinned_node_client(
        locator,
        network_backend=backend,
    ) as client:
        response = await client.get(
            "/fleet/v1/snapshot",
            headers={"Authorization": "Bearer private-discovery-secret"},
        )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert [call[:2] for call in backend.calls] == [
        ("10.20.30.39", 1240),
        ("10.20.30.40", 1240),
    ]
    assert all(call[2] is not None and 0 < call[2] <= 5 for call in backend.calls)
    assert len(stream.tls_calls) == 1
    context, server_hostname, _timeout = stream.tls_calls[0]
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert server_hostname == "worker.example.internal"
    wire_request = b"".join(stream.writes)
    assert b"Host: worker.example.internal:1240\r\n" in wire_request
    assert b"Authorization: Bearer private-discovery-secret\r\n" in wire_request


@pytest.mark.parametrize(
    "peer",
    [
        ("10.20.30.41", 1240),
        ("10.20.30.40", 1241),
        None,
        "10.20.30.40",
    ],
)
async def test_peer_is_proven_before_tls_or_credential_headers_are_written(
    peer: Any,
) -> None:
    locator = _locator()
    stream = _ScriptedStream(_response(), peer=peer)
    backend = _ScriptedBackend({"10.20.30.40": stream})

    async with create_pinned_node_client(
        locator,
        network_backend=backend,
    ) as client:
        with pytest.raises(httpx.ConnectError) as captured:
            await client.get(
                "/v1/models",
                headers={"Authorization": "Bearer must-never-leave"},
            )

    assert str(captured.value) == "paired_peer_mismatch"
    assert "must-never-leave" not in str(captured.value)
    assert stream.closed
    assert stream.tls_calls == []
    assert stream.writes == []


async def test_trusted_lan_http_streams_without_tls_after_peer_proof() -> None:
    locator = _locator(
        origin="http://mac.lan:1240",
        transport="trusted_lan_http",
        host="mac.lan",
        addresses=("192.168.50.20",),
    )
    stream = _ScriptedStream(
        _response(b"streamed-body"),
        peer=("192.168.50.20", 1240),
    )
    backend = _ScriptedBackend({"192.168.50.20": stream})

    async with create_pinned_node_client(
        locator,
        network_backend=backend,
    ) as client:
        async with client.stream("POST", "/v1/responses", content=b"request") as response:
            chunks = [chunk async for chunk in response.aiter_bytes()]

    assert b"".join(chunks) == b"streamed-body"
    assert stream.tls_calls == []
    assert backend.calls[0][:2] == ("192.168.50.20", 1240)


async def test_redirect_is_terminal_even_when_caller_requests_following() -> None:
    locator = _locator()
    stream = _ScriptedStream(
        _response(
            b"",
            status=302,
            headers=((b"Location", b"http://169.254.169.254/latest"),),
        ),
        peer=("10.20.30.40", 1240),
    )
    backend = _ScriptedBackend({"10.20.30.40": stream})

    async with create_pinned_node_client(
        locator,
        network_backend=backend,
    ) as client:
        with pytest.raises(httpx.RemoteProtocolError) as captured:
            await client.get("/v1/models", follow_redirects=True)

    assert str(captured.value) == "paired_redirect_forbidden"
    assert len(backend.calls) == 1


@pytest.mark.parametrize(
    ("request_mutation", "code"),
    [
        ("origin", "paired_origin_mismatch"),
        ("sni", "paired_sni_override_forbidden"),
        ("target", "paired_target_override_forbidden"),
        ("proxy_header", "paired_proxy_header_forbidden"),
        ("connect", "paired_connect_method_forbidden"),
    ],
)
async def test_request_escape_hatches_fail_before_connect(
    request_mutation: str,
    code: str,
) -> None:
    locator = _locator()
    backend = _ScriptedBackend(
        {
            "10.20.30.40": _ScriptedStream(
                _response(),
                peer=("10.20.30.40", 1240),
            )
        }
    )
    transport = PinnedNodeTransport(locator, network_backend=backend)
    async with httpx.AsyncClient(transport=transport) as client:
        url = (
            "https://other.example/private-value"
            if request_mutation == "origin"
            else f"{locator.origin}/v1/models"
        )
        method = "CONNECT" if request_mutation == "connect" else "GET"
        request = client.build_request(method, url)
        if request_mutation == "sni":
            request.extensions["sni_hostname"] = "other.example"
        if request_mutation == "target":
            request.extensions["target"] = b"http://169.254.169.254/latest"
        if request_mutation == "proxy_header":
            request.headers["Proxy-Authorization"] = "Bearer proxy-secret"

        with pytest.raises(PairedTransportError) as captured:
            await client.send(request)

    assert captured.value.code == code
    assert "private-value" not in str(captured.value)
    assert "proxy-secret" not in str(captured.value)
    assert backend.calls == []


def test_insecure_tls_context_and_malformed_resolved_locators_fail_closed() -> None:
    insecure = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    insecure.check_hostname = False
    insecure.verify_mode = ssl.CERT_NONE
    with pytest.raises(PairedTransportError) as tls_error:
        PinnedNodeTransport(_locator(), ssl_context=insecure)
    assert tls_error.value.code == "paired_tls_verification_required"

    malformed = (
        _locator(origin="https://other.example:1240"),
        _locator(addresses=()),
        _locator(addresses=("010.020.030.040",)),
        _locator(
            origin="http://worker.example.internal:1240",
            transport="https",
        ),
    )
    for locator in malformed:
        with pytest.raises(PairedTransportError) as captured:
            PinnedNodeTransport(locator)
        assert captured.value.code == "paired_locator_invalid"


async def test_tls_context_mutation_is_rechecked_before_any_header_write() -> None:
    locator = _locator()
    stream = _ScriptedStream(
        _response(),
        peer=("10.20.30.40", 1240),
    )
    backend = _ScriptedBackend({"10.20.30.40": stream})
    context = ssl.create_default_context()
    transport = PinnedNodeTransport(
        locator,
        ssl_context=context,
        network_backend=backend,
    )
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(httpx.ConnectError) as captured:
            await client.get(
                f"{locator.origin}/v1/models",
                headers={"Authorization": "Bearer must-never-leave"},
            )

    assert str(captured.value) == "paired_tls_verification_required"
    assert stream.closed
    assert stream.tls_calls == []
    assert stream.writes == []


def test_httpx_and_httpcore_versions_match_the_reviewed_transport_contract() -> None:
    assert httpx.__version__ == "0.28.1"
    assert httpcore.__version__ == "1.0.9"
