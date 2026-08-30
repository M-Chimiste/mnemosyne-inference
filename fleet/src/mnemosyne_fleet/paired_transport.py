"""Peer-pinned HTTP transport for dynamic Fleet enrollments.

HTTPX 0.28.1's public ``AsyncHTTPTransport`` constructor cannot inject a
network backend or a pre-resolved address. This narrowly scoped transport uses
httpcore 1.0.9's public ``AsyncConnectionPool(network_backend=...)`` boundary
so TCP can be pinned without replacing HTTP parsing, pooling, TLS, or streaming.
The dependency and its reviewed versions are locked by the Fleet package.
"""

from __future__ import annotations

import contextlib
import ipaddress
import ssl
import time
from collections.abc import AsyncIterable, AsyncIterator, Iterable, Iterator
from types import TracebackType
from typing import Any

import httpcore
import httpx

from .locator_policy import ResolvedLocator


_DEFAULT_LIMITS = httpx.Limits(
    max_connections=100,
    max_keepalive_connections=20,
    keepalive_expiry=5.0,
)
_DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=5.0)
_FORBIDDEN_REQUEST_HEADERS = frozenset(
    {
        b"proxy-authorization",
        b"proxy-connection",
    }
)


class PairedTransportError(RuntimeError):
    """Fixed-code construction failure for the pinned paired-node transport."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _PinnedPeerStream(httpcore.AsyncNetworkStream):
    """Preserve peer proof while enforcing TLS at the last possible boundary."""

    def __init__(
        self,
        stream: httpcore.AsyncNetworkStream,
        locator: ResolvedLocator,
    ) -> None:
        self._stream = stream
        self._locator = locator

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return await self._stream.read(max_bytes, timeout)

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        await self._stream.write(buffer, timeout)

    async def aclose(self) -> None:
        await self._stream.aclose()

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.AsyncNetworkStream:
        if (
            ssl_context.verify_mode != ssl.CERT_REQUIRED
            or not ssl_context.check_hostname
            or server_hostname != self._locator.host
        ):
            try:
                await self._stream.aclose()
            except Exception:
                pass
            raise httpcore.ConnectError("paired_tls_verification_required")
        return await self._stream.start_tls(
            ssl_context,
            server_hostname=server_hostname,
            timeout=timeout,
        )

    def get_extra_info(self, info: str) -> Any:
        return self._stream.get_extra_info(info)


class _PinnedNetworkBackend(httpcore.AsyncNetworkBackend):
    """Connect an httpcore origin to one already-approved numeric address.

    httpcore still owns TLS and receives the original origin hostname. This
    backend changes only the TCP destination, then proves the socket peer
    before returning the stream. Consequently HTTP headers cannot be written
    until peer pinning has succeeded, and HTTPS keeps ordinary hostname/SNI
    and certificate validation.
    """

    def __init__(
        self,
        locator: ResolvedLocator,
        *,
        backend: httpcore.AsyncNetworkBackend | None = None,
        connect_timeout_ceiling_seconds: float = 5.0,
    ) -> None:
        if (
            isinstance(connect_timeout_ceiling_seconds, bool)
            or not isinstance(connect_timeout_ceiling_seconds, (int, float))
            or connect_timeout_ceiling_seconds <= 0
            or connect_timeout_ceiling_seconds > 30
        ):
            raise PairedTransportError("paired_connect_timeout_invalid")
        self._locator = locator
        self._backend = backend or httpcore.AnyIOBackend()
        self._connect_timeout_ceiling_seconds = float(
            connect_timeout_ceiling_seconds
        )

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        if host != self._locator.host or port != self._locator.port:
            raise httpcore.ConnectError("paired_origin_mismatch")

        budget = self._connect_timeout_ceiling_seconds
        if timeout is not None:
            budget = min(budget, max(0.0, float(timeout)))
        deadline = time.monotonic() + budget
        saw_connect_error = False

        for address in self._locator.addresses:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                stream = await self._backend.connect_tcp(
                    address,
                    port,
                    timeout=remaining,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except httpcore.ConnectTimeout:
                continue
            except httpcore.ConnectError:
                saw_connect_error = True
                continue

            if not _stream_peer_matches(stream, self._locator):
                try:
                    await stream.aclose()
                except Exception:
                    pass
                raise httpcore.ConnectError("paired_peer_mismatch")
            return _PinnedPeerStream(stream, self._locator)

        if saw_connect_error:
            raise httpcore.ConnectError("paired_connect_failed")
        raise httpcore.ConnectTimeout("paired_connect_timeout")

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        del path, timeout, socket_options
        raise httpcore.ConnectError("paired_unix_socket_forbidden")

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


class _AsyncResponseStream(httpx.AsyncByteStream):
    def __init__(self, stream: AsyncIterable[bytes]) -> None:
        self._stream = stream

    async def __aiter__(self) -> AsyncIterator[bytes]:
        with _map_httpcore_exceptions():
            async for part in self._stream:
                yield part

    async def aclose(self) -> None:
        if hasattr(self._stream, "aclose"):
            with _map_httpcore_exceptions():
                await self._stream.aclose()


class PinnedNodeTransport(httpx.AsyncBaseTransport):
    """An HTTPX transport pinned to one policy-approved paired-node origin.

    This transport deliberately has no proxy support, never performs hostname
    resolution, rejects a request for any other origin, and treats redirects
    as protocol errors. For HTTPS, the connection pool retains the original
    hostname so normal SNI and certificate hostname verification remain in
    force while TCP connects only to a pinned numeric address.

    Construct a new instance from a freshly resolved locator when the approved
    DNS result changes. Do not mutate or reuse it for another enrollment.
    """

    def __init__(
        self,
        locator: ResolvedLocator,
        *,
        ssl_context: ssl.SSLContext | None = None,
        limits: httpx.Limits = _DEFAULT_LIMITS,
        http2: bool = False,
        connect_timeout_ceiling_seconds: float = 5.0,
        network_backend: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        _validate_resolved_locator(locator)
        context = ssl_context or ssl.create_default_context(
            purpose=ssl.Purpose.SERVER_AUTH
        )
        if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
            raise PairedTransportError("paired_tls_verification_required")

        self._locator = locator
        self._scheme = locator.origin.split(":", 1)[0]
        self._authority = _authority(locator).encode("ascii")
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=context,
            proxy=None,
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            http1=True,
            http2=http2,
            retries=0,
            network_backend=_PinnedNetworkBackend(
                locator,
                backend=network_backend,
                connect_timeout_ceiling_seconds=connect_timeout_ceiling_seconds,
            ),
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self._validate_request(request)
        if not isinstance(request.stream, httpx.AsyncByteStream):
            raise PairedTransportError("paired_async_stream_required")

        raw_headers: list[tuple[bytes, bytes]] = [(b"Host", self._authority)]
        for name, value in request.headers.raw:
            lower_name = name.lower()
            if lower_name == b"host":
                continue
            if lower_name in _FORBIDDEN_REQUEST_HEADERS:
                raise PairedTransportError("paired_proxy_header_forbidden")
            raw_headers.append((name, value))

        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=raw_headers,
            content=request.stream,
            extensions=request.extensions,
        )
        with _map_httpcore_exceptions():
            response = await self._pool.handle_async_request(core_request)

        if 300 <= response.status < 400 and any(
            name.lower() == b"location" for name, _value in response.headers
        ):
            await response.stream.aclose()
            raise httpx.RemoteProtocolError("paired_redirect_forbidden")

        if not isinstance(response.stream, AsyncIterable):
            # httpcore guarantees an async iterable here. Keep a fixed failure
            # in case that contract changes rather than buffering a response.
            await response.aclose()
            raise PairedTransportError("paired_response_stream_invalid")

        return httpx.Response(
            status_code=response.status,
            headers=response.headers,
            stream=_AsyncResponseStream(response.stream),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self._pool.aclose()

    async def __aenter__(self) -> PinnedNodeTransport:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        await self.aclose()

    def _validate_request(self, request: httpx.Request) -> None:
        scheme = request.url.raw_scheme.decode("ascii", errors="strict")
        host = request.url.raw_host.decode("ascii", errors="strict")
        port = request.url.port or {"http": 80, "https": 443}.get(scheme)
        if (
            scheme != self._scheme
            or host != self._locator.host
            or port != self._locator.port
            or request.url.userinfo
        ):
            raise PairedTransportError("paired_origin_mismatch")
        if "sni_hostname" in request.extensions:
            raise PairedTransportError("paired_sni_override_forbidden")
        if "target" in request.extensions:
            raise PairedTransportError("paired_target_override_forbidden")
        if request.method.upper() == "CONNECT":
            raise PairedTransportError("paired_connect_method_forbidden")


def create_pinned_node_client(
    locator: ResolvedLocator,
    *,
    timeout: httpx.Timeout | float = _DEFAULT_TIMEOUT,
    limits: httpx.Limits = _DEFAULT_LIMITS,
    ssl_context: ssl.SSLContext | None = None,
    http2: bool = False,
    connect_timeout_ceiling_seconds: float = 5.0,
    network_backend: httpcore.AsyncNetworkBackend | None = None,
) -> httpx.AsyncClient:
    """Return a no-proxy, no-redirect streaming client for one paired node.

    The returned client accepts relative paths against ``locator.origin`` or
    exact-origin absolute URLs. The transport independently rejects redirects
    and origin changes, so per-request redirect options cannot bypass pinning.
    """

    transport = PinnedNodeTransport(
        locator,
        ssl_context=ssl_context,
        limits=limits,
        http2=http2,
        connect_timeout_ceiling_seconds=connect_timeout_ceiling_seconds,
        network_backend=network_backend,
    )
    return httpx.AsyncClient(
        base_url=locator.origin,
        transport=transport,
        timeout=timeout,
        trust_env=False,
        follow_redirects=False,
    )


def _validate_resolved_locator(locator: ResolvedLocator) -> None:
    if not isinstance(locator, ResolvedLocator):
        raise PairedTransportError("paired_locator_invalid")
    if not locator.addresses or len(locator.addresses) > 256:
        raise PairedTransportError("paired_locator_invalid")
    if locator.port < 1 or locator.port > 65535:
        raise PairedTransportError("paired_locator_invalid")

    scheme = locator.origin.split(":", 1)[0]
    allowed_schemes = {
        "https": frozenset({"https"}),
        "tailscale": frozenset({"http", "https"}),
        "trusted_lan_http": frozenset({"http"}),
    }.get(locator.transport)
    if allowed_schemes is None or scheme not in allowed_schemes:
        raise PairedTransportError("paired_locator_invalid")

    expected_origin = f"{scheme}://{_authority(locator)}"
    if locator.origin != expected_origin:
        raise PairedTransportError("paired_locator_invalid")

    seen: set[str] = set()
    for address in locator.addresses:
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError as exc:
            raise PairedTransportError("paired_locator_invalid") from exc
        if str(parsed) != address or address in seen:
            raise PairedTransportError("paired_locator_invalid")
        seen.add(address)


def _authority(locator: ResolvedLocator) -> str:
    try:
        address = ipaddress.ip_address(locator.host)
    except ValueError:
        rendered_host = locator.host
    else:
        rendered_host = f"[{locator.host}]" if address.version == 6 else locator.host
    return f"{rendered_host}:{locator.port}"


def _stream_peer_matches(
    stream: httpcore.AsyncNetworkStream,
    locator: ResolvedLocator,
) -> bool:
    peer: Any = stream.get_extra_info("server_addr")
    if (
        not isinstance(peer, (tuple, list))
        or len(peer) < 2
        or not isinstance(peer[0], str)
        or isinstance(peer[1], bool)
        or not isinstance(peer[1], int)
        or peer[1] != locator.port
    ):
        return False
    return locator.permits_peer(peer[0])


_HTTPCORE_EXCEPTIONS: tuple[tuple[type[Exception], type[httpx.HTTPError]], ...] = (
    (httpcore.ConnectTimeout, httpx.ConnectTimeout),
    (httpcore.ReadTimeout, httpx.ReadTimeout),
    (httpcore.WriteTimeout, httpx.WriteTimeout),
    (httpcore.PoolTimeout, httpx.PoolTimeout),
    (httpcore.ConnectError, httpx.ConnectError),
    (httpcore.ReadError, httpx.ReadError),
    (httpcore.WriteError, httpx.WriteError),
    (httpcore.ProxyError, httpx.ProxyError),
    (httpcore.UnsupportedProtocol, httpx.UnsupportedProtocol),
    (httpcore.LocalProtocolError, httpx.LocalProtocolError),
    (httpcore.RemoteProtocolError, httpx.RemoteProtocolError),
    (httpcore.ProtocolError, httpx.ProtocolError),
    (httpcore.TimeoutException, httpx.TimeoutException),
    (httpcore.NetworkError, httpx.NetworkError),
)


@contextlib.contextmanager
def _map_httpcore_exceptions() -> Iterator[None]:
    try:
        yield
    except Exception as exc:
        for source, destination in _HTTPCORE_EXCEPTIONS:
            if isinstance(exc, source):
                raise destination(str(exc)) from exc
        raise
