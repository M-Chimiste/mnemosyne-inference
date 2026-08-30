from __future__ import annotations

import asyncio
import inspect
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, TypeAlias, cast
from urllib.parse import SplitResult, urlsplit


LocatorTransport = Literal["https", "tailscale", "trusted_lan_http"]
LocatorTarget = Literal["remote_mac", "nyx_local_worker"]
IPAddress: TypeAlias = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork: TypeAlias = ipaddress.IPv4Network | ipaddress.IPv6Network
ResolverResult: TypeAlias = Sequence[str] | Iterable[str]
AddressResolver: TypeAlias = Callable[
    [str, int], ResolverResult | Awaitable[ResolverResult]
]

TRANSPORTS: tuple[LocatorTransport, ...] = (
    "https",
    "tailscale",
    "trusted_lan_http",
)

_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_NUMERIC_LABEL = re.compile(r"(?:0[xX][0-9a-fA-F]+|[0-9]+)\Z")
_METADATA_ADDRESSES = frozenset(
    {
        ipaddress.ip_address("100.100.100.200"),
        ipaddress.ip_address("169.254.169.254"),
        ipaddress.ip_address("169.254.170.2"),
        ipaddress.ip_address("fd00:ec2::254"),
    }
)


class LocatorPolicyError(ValueError):
    """Fixed-code failure for an unsafe or unresolvable node locator."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ResolvedLocator:
    """Canonical, credential-free connection target approved by the policy."""

    origin: str
    transport: LocatorTransport
    host: str
    port: int
    addresses: tuple[str, ...]

    def permits_peer(self, peer_address: str) -> bool:
        """Return whether a connected peer matches this exact DNS resolution."""

        try:
            peer = _parse_resolved_address(peer_address)
        except LocatorPolicyError:
            return False
        return str(peer) in self.addresses


@dataclass(frozen=True, slots=True)
class _NormalizedOrigin:
    origin: str
    transport: LocatorTransport
    host: str
    port: int
    literal_address: IPAddress | None


class LocatorPolicy:
    """Normalize and resolve pairing locators under explicit network policy.

    The resolver is injectable so callers can use a connection-pinning DNS
    implementation and tests never depend on ambient DNS. The default system
    resolver is run off the event loop and bounded by ``resolution_timeout``.
    Callers must resolve again immediately before connecting and verify the
    socket peer with :meth:`validate_peer`.
    """

    def __init__(
        self,
        *,
        cidr_allowlists: Mapping[LocatorTransport, Sequence[str | IPNetwork]],
        allowed_ports: Sequence[int],
        resolution_timeout_seconds: float = 5.0,
        max_resolved_addresses: int = 32,
        resolver: AddressResolver | None = None,
    ) -> None:
        if (
            isinstance(resolution_timeout_seconds, bool)
            or resolution_timeout_seconds <= 0
            or resolution_timeout_seconds > 30
        ):
            raise ValueError("resolution timeout must be greater than zero and at most 30")
        if (
            isinstance(max_resolved_addresses, bool)
            or max_resolved_addresses < 1
            or max_resolved_addresses > 256
        ):
            raise ValueError("max resolved addresses must be between 1 and 256")

        ports: set[int] = set()
        for port in allowed_ports:
            if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
                raise ValueError("allowed ports must contain integers from 1 through 65535")
            if port in ports:
                raise ValueError("allowed ports must be unique")
            ports.add(port)

        allowlists: dict[LocatorTransport, tuple[IPNetwork, ...]] = {}
        unknown_transports = set(cidr_allowlists) - set(TRANSPORTS)
        if unknown_transports:
            raise ValueError("CIDR allowlists contain an unsupported transport")
        for transport in TRANSPORTS:
            networks: list[IPNetwork] = []
            seen_networks: set[tuple[int, int, int]] = set()
            for raw_network in cidr_allowlists.get(transport, ()):
                try:
                    network = (
                        raw_network
                        if isinstance(
                            raw_network,
                            (ipaddress.IPv4Network, ipaddress.IPv6Network),
                        )
                        else ipaddress.ip_network(raw_network, strict=True)
                    )
                except (TypeError, ValueError) as exc:
                    raise ValueError("CIDR allowlists must contain canonical networks") from exc
                key = (network.version, int(network.network_address), network.prefixlen)
                if key in seen_networks:
                    raise ValueError("CIDR allowlists must not contain duplicates")
                seen_networks.add(key)
                networks.append(network)
            allowlists[transport] = tuple(networks)

        self._allowlists = allowlists
        self._allowed_ports = frozenset(ports)
        self._resolution_timeout_seconds = float(resolution_timeout_seconds)
        self._max_resolved_addresses = max_resolved_addresses
        self._resolver = resolver

    async def resolve(
        self,
        raw_origin: str,
        *,
        transport: LocatorTransport,
        target: LocatorTarget = "remote_mac",
    ) -> ResolvedLocator:
        normalized = self.normalize(raw_origin, transport=transport)
        if target not in {"remote_mac", "nyx_local_worker"}:
            raise LocatorPolicyError("locator_target_invalid")

        networks = self._allowlists[transport]
        if not networks:
            raise LocatorPolicyError("locator_transport_not_configured")

        if normalized.literal_address is not None:
            raw_addresses: Iterable[str] = (str(normalized.literal_address),)
        else:
            raw_addresses = await self._resolve_host(normalized.host, normalized.port)

        parsed_addresses = self._bounded_parse_addresses(raw_addresses)
        allowed: list[IPAddress] = []
        disallowed = False
        for address in parsed_addresses:
            self._reject_special_address(address, target=target, networks=networks)
            if any(
                address.version == network.version and address in network
                for network in networks
            ):
                allowed.append(address)
            else:
                disallowed = True

        if disallowed:
            code = "locator_mixed_resolution" if allowed else "locator_address_not_allowed"
            raise LocatorPolicyError(code)
        if not allowed:
            raise LocatorPolicyError("locator_resolution_empty")

        addresses = tuple(
            str(address)
            for address in sorted(
                set(allowed),
                key=lambda value: (value.version, int(value)),
            )
        )
        return ResolvedLocator(
            origin=normalized.origin,
            transport=normalized.transport,
            host=normalized.host,
            port=normalized.port,
            addresses=addresses,
        )

    def normalize(
        self,
        raw_origin: str,
        *,
        transport: LocatorTransport,
    ) -> _NormalizedOrigin:
        if transport not in TRANSPORTS:
            raise LocatorPolicyError("locator_transport_invalid")
        if not isinstance(raw_origin, str):
            raise LocatorPolicyError("locator_invalid")
        try:
            encoded_length = len(raw_origin.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise LocatorPolicyError("locator_invalid") from exc
        if (
            encoded_length == 0
            or encoded_length > 2048
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in raw_origin)
            or any(character.isspace() for character in raw_origin)
            or "\\" in raw_origin
        ):
            raise LocatorPolicyError("locator_invalid")

        try:
            parsed = urlsplit(raw_origin)
        except ValueError as exc:
            raise LocatorPolicyError("locator_invalid") from exc
        self._validate_origin_shape(parsed)

        scheme = parsed.scheme.lower()
        allowed_schemes = {
            "https": frozenset({"https"}),
            "tailscale": frozenset({"http", "https"}),
            "trusted_lan_http": frozenset({"http"}),
        }[transport]
        if scheme not in allowed_schemes:
            raise LocatorPolicyError("locator_transport_scheme_mismatch")

        try:
            port = parsed.port
        except ValueError as exc:
            raise LocatorPolicyError("locator_port_invalid") from exc
        if port is None:
            raise LocatorPolicyError("locator_port_required")
        if port not in self._allowed_ports:
            raise LocatorPolicyError("locator_port_not_allowed")

        raw_host = parsed.hostname
        if raw_host is None:
            raise LocatorPolicyError("locator_host_invalid")
        host, literal_address = _normalize_host(raw_host)
        rendered_host = f"[{host}]" if literal_address is not None and literal_address.version == 6 else host
        return _NormalizedOrigin(
            origin=f"{scheme}://{rendered_host}:{port}",
            transport=transport,
            host=host,
            port=port,
            literal_address=literal_address,
        )

    def validate_peer(self, locator: ResolvedLocator, peer_address: str) -> None:
        """Fail unless a connection reached one address in the pinned result."""

        if not locator.permits_peer(peer_address):
            raise LocatorPolicyError("locator_peer_mismatch")

    @staticmethod
    def _validate_origin_shape(parsed: SplitResult) -> None:
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or "@" in parsed.netloc
            or "%" in parsed.netloc
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise LocatorPolicyError("locator_invalid")

    async def _resolve_host(self, host: str, port: int) -> ResolverResult:
        resolver = self._resolver
        try:
            if resolver is None:
                pending: Awaitable[ResolverResult] = asyncio.to_thread(
                    _system_resolve,
                    host,
                    port,
                )
            else:
                value = resolver(host, port)
                pending = (
                    cast(Awaitable[ResolverResult], value)
                    if inspect.isawaitable(value)
                    else _immediate(value)
                )
            return await asyncio.wait_for(
                pending,
                timeout=self._resolution_timeout_seconds,
            )
        except TimeoutError as exc:
            raise LocatorPolicyError("locator_resolution_timeout") from exc
        except LocatorPolicyError:
            raise
        except Exception as exc:
            raise LocatorPolicyError("locator_resolution_failed") from exc

    def _bounded_parse_addresses(self, raw_addresses: Iterable[str]) -> tuple[IPAddress, ...]:
        addresses: list[IPAddress] = []
        try:
            iterator = iter(raw_addresses)
        except TypeError as exc:
            raise LocatorPolicyError("locator_resolution_failed") from exc
        for index, raw_address in enumerate(iterator):
            if index >= self._max_resolved_addresses:
                raise LocatorPolicyError("locator_resolution_too_large")
            addresses.append(_parse_resolved_address(raw_address))
        if not addresses:
            raise LocatorPolicyError("locator_resolution_empty")
        return tuple(addresses)

    @staticmethod
    def _reject_special_address(
        address: IPAddress,
        *,
        target: LocatorTarget,
        networks: Sequence[IPNetwork],
    ) -> None:
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            raise LocatorPolicyError("locator_address_prohibited")
        if address in _METADATA_ADDRESSES:
            raise LocatorPolicyError("locator_address_prohibited")
        if address.is_unspecified or address.is_multicast or address.is_link_local:
            raise LocatorPolicyError("locator_address_prohibited")
        if address.is_loopback and target != "nyx_local_worker":
            raise LocatorPolicyError("locator_address_prohibited")
        if isinstance(address, ipaddress.IPv4Address):
            if address == ipaddress.IPv4Address("255.255.255.255"):
                raise LocatorPolicyError("locator_address_prohibited")
            for network in networks:
                if (
                    isinstance(network, ipaddress.IPv4Network)
                    and network.prefixlen <= 30
                    and address == network.broadcast_address
                ):
                    raise LocatorPolicyError("locator_address_prohibited")


async def _immediate(value: ResolverResult) -> ResolverResult:
    return value


def _system_resolve(host: str, port: int) -> tuple[str, ...]:
    rows = socket.getaddrinfo(
        host,
        port,
        family=socket.AF_UNSPEC,
        type=socket.SOCK_STREAM,
        proto=socket.IPPROTO_TCP,
    )
    return tuple(row[4][0] for row in rows)


def _normalize_host(raw_host: str) -> tuple[str, IPAddress | None]:
    if (
        not raw_host
        or len(raw_host) > 253
        or any(ord(character) > 0x7F for character in raw_host)
        or "%" in raw_host
    ):
        raise LocatorPolicyError("locator_host_invalid")
    host = raw_host.lower()
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        return str(literal), literal

    if _looks_like_ambiguous_ip(host):
        raise LocatorPolicyError("locator_host_ambiguous")
    if host.endswith("."):
        host = host[:-1]
    labels = host.split(".")
    if not host or any(_DNS_LABEL.fullmatch(label) is None for label in labels):
        raise LocatorPolicyError("locator_host_invalid")
    return host, None


def _looks_like_ambiguous_ip(host: str) -> bool:
    candidate = host[:-1] if host.endswith(".") else host
    labels = candidate.split(".")
    return bool(labels) and all(_NUMERIC_LABEL.fullmatch(label) is not None for label in labels)


def _parse_resolved_address(raw_address: str) -> IPAddress:
    if not isinstance(raw_address, str) or "%" in raw_address:
        raise LocatorPolicyError("locator_resolution_invalid")
    try:
        return ipaddress.ip_address(raw_address)
    except ValueError as exc:
        raise LocatorPolicyError("locator_resolution_invalid") from exc
