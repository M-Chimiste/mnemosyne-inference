from __future__ import annotations

import asyncio

import pytest

from mnemosyne_fleet.locator_policy import (
    LocatorPolicy,
    LocatorPolicyError,
)


def _policy(
    *,
    https: tuple[str, ...] = ("10.0.0.0/8",),
    tailscale: tuple[str, ...] = ("100.64.0.0/10", "fd7a:115c:a1e0::/48"),
    trusted_lan_http: tuple[str, ...] = ("192.168.0.0/16",),
    ports: tuple[int, ...] = (1240, 443),
    resolver=None,
    timeout: float = 1.0,
    max_addresses: int = 32,
) -> LocatorPolicy:
    return LocatorPolicy(
        cidr_allowlists={
            "https": https,
            "tailscale": tailscale,
            "trusted_lan_http": trusted_lan_http,
        },
        allowed_ports=ports,
        resolution_timeout_seconds=timeout,
        max_resolved_addresses=max_addresses,
        resolver=resolver,
    )


async def test_dns_origin_is_canonicalized_and_every_address_is_retained() -> None:
    calls: list[tuple[str, int]] = []

    async def resolver(host: str, port: int) -> tuple[str, ...]:
        calls.append((host, port))
        return ("10.2.3.5", "10.2.3.4", "10.2.3.5")

    resolved = await _policy(resolver=resolver).resolve(
        "HTTPS://MAC-A.EXAMPLE.INTERNAL.:1240/",
        transport="https",
    )

    assert calls == [("mac-a.example.internal", 1240)]
    assert resolved.origin == "https://mac-a.example.internal:1240"
    assert resolved.host == "mac-a.example.internal"
    assert resolved.port == 1240
    assert resolved.transport == "https"
    assert resolved.addresses == ("10.2.3.4", "10.2.3.5")


async def test_ipv6_literal_is_canonical_path_safe_and_does_not_use_dns() -> None:
    def resolver(_host: str, _port: int):
        raise AssertionError("IP literals must not invoke DNS")

    resolved = await _policy(
        https=("fd12:3456:789a::/48",),
        resolver=resolver,
    ).resolve(
        "https://[fd12:3456:789a:0:0:0:0:4]:1240/",
        transport="https",
    )

    assert resolved.origin == "https://[fd12:3456:789a::4]:1240"
    assert resolved.host == "fd12:3456:789a::4"
    assert resolved.addresses == ("fd12:3456:789a::4",)


@pytest.mark.parametrize(
    ("transport", "scheme", "accepted"),
    [
        ("https", "https", True),
        ("https", "http", False),
        ("tailscale", "https", True),
        ("tailscale", "http", True),
        ("trusted_lan_http", "http", True),
        ("trusted_lan_http", "https", False),
    ],
)
async def test_declared_transport_controls_the_only_accepted_schemes(
    transport: str,
    scheme: str,
    accepted: bool,
) -> None:
    address = {
        "https": "10.1.2.3",
        "tailscale": "100.64.1.2",
        "trusted_lan_http": "192.168.1.2",
    }[transport]
    policy = _policy(resolver=lambda _host, _port: (address,))
    if accepted:
        resolved = await policy.resolve(
            f"{scheme}://worker.example:1240",
            transport=transport,  # type: ignore[arg-type]
        )
        assert resolved.origin == f"{scheme}://worker.example:1240"
    else:
        with pytest.raises(LocatorPolicyError) as captured:
            await policy.resolve(
                f"{scheme}://worker.example:1240",
                transport=transport,  # type: ignore[arg-type]
            )
        assert captured.value.code == "locator_transport_scheme_mismatch"


@pytest.mark.parametrize(
    "origin",
    [
        "https://user@worker.example:1240",
        "https://worker.example:1240/models",
        "https://worker.example:1240?next=http://metadata",
        "https://worker.example:1240#fragment",
        "https://%31%30.0.0.1:1240",
        "https://worker.example:1240\\@10.0.0.1",
        "https://worker.example:1240\n",
        " https://worker.example:1240",
        "https://[fe80::1%25en0]:1240",
    ],
)
async def test_authority_path_and_control_character_ambiguities_are_rejected(
    origin: str,
) -> None:
    with pytest.raises(LocatorPolicyError) as captured:
        await _policy(resolver=lambda _host, _port: ("10.1.2.3",)).resolve(
            origin,
            transport="https",
        )
    assert captured.value.code == "locator_invalid"


@pytest.mark.parametrize(
    "host",
    [
        "127.1",
        "2130706433",
        "0x7f000001",
        "0177.0.0.1",
    ],
)
async def test_ambiguous_ipv4_spellings_are_rejected_before_dns(host: str) -> None:
    with pytest.raises(LocatorPolicyError) as captured:
        await _policy(resolver=lambda _host, _port: ("10.1.2.3",)).resolve(
            f"https://{host}:1240",
            transport="https",
        )
    assert captured.value.code == "locator_host_ambiguous"


@pytest.mark.parametrize(
    ("origin", "code"),
    [
        ("https://worker.example", "locator_port_required"),
        ("https://worker.example:8443", "locator_port_not_allowed"),
        ("https://worker.example:99999", "locator_port_invalid"),
    ],
)
async def test_port_must_be_explicit_and_allowlisted(origin: str, code: str) -> None:
    with pytest.raises(LocatorPolicyError) as captured:
        await _policy(resolver=lambda _host, _port: ("10.1.2.3",)).resolve(
            origin,
            transport="https",
        )
    assert captured.value.code == code


async def test_mixed_allowed_and_disallowed_dns_answers_fail_closed() -> None:
    policy = _policy(
        resolver=lambda _host, _port: ("10.1.2.3", "192.168.1.2"),
    )
    with pytest.raises(LocatorPolicyError) as captured:
        await policy.resolve("https://worker.example:1240", transport="https")
    assert captured.value.code == "locator_mixed_resolution"


@pytest.mark.parametrize(
    ("address", "allowlist"),
    [
        ("0.0.0.0", ("0.0.0.0/0",)),
        ("224.0.0.1", ("224.0.0.0/4",)),
        ("169.254.1.2", ("169.254.0.0/16",)),
        ("100.100.100.200", ("100.64.0.0/10",)),
        ("127.0.0.1", ("127.0.0.0/8",)),
        ("10.1.2.255", ("10.1.2.0/24",)),
        ("::", ("::/0",)),
        ("ff02::1", ("ff00::/8",)),
        ("fe80::1", ("fe80::/10",)),
        ("::ffff:10.1.2.3", ("::/0",)),
        ("fd00:ec2::254", ("fd00::/8",)),
    ],
)
async def test_special_and_metadata_addresses_are_denied_even_when_allowlisted(
    address: str,
    allowlist: tuple[str, ...],
) -> None:
    with pytest.raises(LocatorPolicyError) as captured:
        await _policy(
            https=allowlist,
            resolver=lambda _host, _port: (address,),
        ).resolve("https://worker.example:1240", transport="https")
    assert captured.value.code == "locator_address_prohibited"


async def test_loopback_requires_both_nyx_local_target_and_explicit_allowlist() -> None:
    policy = _policy(https=("127.0.0.0/8",))
    resolved = await policy.resolve(
        "https://127.0.0.1:1240",
        transport="https",
        target="nyx_local_worker",
    )
    assert resolved.addresses == ("127.0.0.1",)

    with pytest.raises(LocatorPolicyError) as captured:
        await _policy(https=("10.0.0.0/8",)).resolve(
            "https://127.0.0.1:1240",
            transport="https",
            target="nyx_local_worker",
        )
    assert captured.value.code == "locator_address_not_allowed"


async def test_empty_transport_allowlist_is_a_closed_transport() -> None:
    with pytest.raises(LocatorPolicyError) as captured:
        await _policy(https=()).resolve(
            "https://10.1.2.3:1240",
            transport="https",
        )
    assert captured.value.code == "locator_transport_not_configured"


async def test_dns_resolution_is_time_bounded() -> None:
    async def blocked(_host: str, _port: int) -> tuple[str, ...]:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    with pytest.raises(LocatorPolicyError) as captured:
        await _policy(resolver=blocked, timeout=0.01).resolve(
            "https://worker.example:1240",
            transport="https",
        )
    assert captured.value.code == "locator_resolution_timeout"


async def test_dns_answer_count_and_shapes_are_bounded() -> None:
    with pytest.raises(LocatorPolicyError) as too_large:
        await _policy(
            resolver=lambda _host, _port: ("10.0.0.1", "10.0.0.2"),
            max_addresses=1,
        ).resolve("https://worker.example:1240", transport="https")
    assert too_large.value.code == "locator_resolution_too_large"

    with pytest.raises(LocatorPolicyError) as invalid:
        await _policy(
            resolver=lambda _host, _port: ("10.0.0.1%en0",),
        ).resolve("https://worker.example:1240", transport="https")
    assert invalid.value.code == "locator_resolution_invalid"


async def test_connected_peer_must_match_the_exact_pinned_resolution() -> None:
    policy = _policy(
        resolver=lambda _host, _port: ("10.2.3.4", "10.2.3.5"),
    )
    resolved = await policy.resolve(
        "https://worker.example:1240",
        transport="https",
    )

    policy.validate_peer(resolved, "10.2.3.5")
    with pytest.raises(LocatorPolicyError) as captured:
        policy.validate_peer(resolved, "10.2.3.6")
    assert captured.value.code == "locator_peer_mismatch"


async def test_policy_errors_do_not_echo_the_topology_sensitive_raw_locator() -> None:
    raw = "https://user:private-value@worker.example:1240/private-value"
    with pytest.raises(LocatorPolicyError) as captured:
        await _policy().resolve(raw, transport="https")
    assert "private-value" not in str(captured.value)
    assert "private-value" not in repr(captured.value)
