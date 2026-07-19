from __future__ import annotations

import httpx
import pytest

from mnemosyne_macos.config import MFluxConfig
from mnemosyne_macos.engines.base import Deadline
from mnemosyne_macos.engines.mflux import MFluxAdapter
from mnemosyne_macos.models import EngineName, ServiceState


@pytest.mark.asyncio
async def test_worker_environment_isolated_from_service_python(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYTHONHOME", "/service/runtime")
    monkeypatch.setenv("PYTHONPATH", "/service/source:/service/site-packages")
    monkeypatch.setenv("MNEMOSYNE_MFLUX_PYTHONPATH", "/image/worker/source")

    client = httpx.AsyncClient()
    adapter = MFluxAdapter(MFluxConfig(enabled=True), client=client)
    try:
        environment = adapter._environment()
        assert "PYTHONHOME" not in environment
        assert environment["PYTHONPATH"] == "/image/worker/source"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_connection_refused_is_authoritative_empty_when_no_worker_is_owned() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = MFluxAdapter(MFluxConfig(enabled=True), client=client)
    try:
        snapshot = await adapter.inspect(deadline=Deadline.after(1))
        assert snapshot.authoritative is True
        assert snapshot.empty is True
        assert snapshot.service_state == ServiceState.STOPPED
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_unowned_worker_on_reserved_port_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"service": "mnemosyne-mflux-worker", "loaded": False},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = MFluxAdapter(MFluxConfig(enabled=True), client=client)
    try:
        snapshot = await adapter.inspect(deadline=Deadline.after(1))
        assert snapshot.authoritative is False
        assert snapshot.service_state == ServiceState.INCOMPATIBLE
        assert "not owned" in (snapshot.diagnostic or "")
        assert snapshot.engine == EngineName.MFLUX
    finally:
        await client.aclose()
