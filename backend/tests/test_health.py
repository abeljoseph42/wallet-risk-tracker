from collections.abc import AsyncIterator, Iterator
from unittest.mock import AsyncMock

import httpx
import pytest

from app.db.session import get_session
from app.main import app


def _override_session(session: AsyncMock) -> None:
    async def _dep() -> AsyncIterator[AsyncMock]:
        yield session

    app.dependency_overrides[get_session] = _dep


@pytest.fixture(autouse=True)
def _clear_overrides() -> Iterator[None]:
    yield
    app.dependency_overrides.clear()


async def _get_health(path: str = "/api/v1/health") -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


async def test_health_ok_when_database_reachable() -> None:
    _override_session(AsyncMock())

    resp = await _get_health()

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "database": "ok"}


async def test_health_degraded_when_database_unreachable() -> None:
    session = AsyncMock()
    session.execute.side_effect = OSError("connection refused")
    _override_session(session)

    resp = await _get_health()

    assert resp.status_code == 503
    assert resp.json() == {"status": "degraded", "database": "unavailable"}


async def test_bare_health_route_serves_container_healthchecks() -> None:
    _override_session(AsyncMock())

    resp = await _get_health("/health")

    assert resp.status_code == 200
