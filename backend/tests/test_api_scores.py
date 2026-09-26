"""Integration tests: the real app, a real Postgres, and a mocked Etherscan."""

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clients.etherscan import EtherscanClient
from app.config import load_scoring_params
from app.core.addresses import to_checksum_address
from app.core.rate_limiter import TokenBucketRateLimiter
from app.db.models import ScoreRun
from app.main import create_app
from app.services.cache import TransactionCache
from app.services.labels import LabelRecord, sync_labels
from app.services.score_jobs import ScoreJobs

TARGET = "0x" + "1" * 40
FUNDER = "0x" + "2" * 40
SANCTIONED = "0x" + "5" * 40
ETH = str(10**18)


def _tx(n: int, src: str, dst: str) -> dict[str, object]:
    return {
        "hash": f"0x{n:064x}",
        "blockNumber": str(100 + n),
        "timeStamp": str(1_700_000_000 + n),
        "from": src,
        "to": dst,
        "value": ETH,
        "isError": "0",
    }


# TARGET received 1 ETH from FUNDER and sent 1 ETH to SANCTIONED.
HISTORIES = {
    ("txlist", TARGET): [_tx(1, FUNDER, TARGET), _tx(2, TARGET, SANCTIONED)],
    ("txlist", FUNDER): [_tx(1, FUNDER, TARGET)],
}


class FakeEtherscan:
    """Serves HISTORIES; can hold requests open, throttle, or fail every request."""

    def __init__(self) -> None:
        self.requests = 0
        self.gate: asyncio.Event | None = None
        self.mode: str = "ok"

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        if self.gate is not None:
            await self.gate.wait()
        if self.mode == "down":
            return httpx.Response(503, text="upstream down")
        if self.mode == "throttle":
            return httpx.Response(
                200,
                json={
                    "status": "0",
                    "message": "NOTOK",
                    "result": "Max calls per sec rate limit reached (3/sec)",
                },
            )
        params = request.url.params
        rows = HISTORIES.get((params["action"], params["address"].lower()), [])
        if not rows:
            return httpx.Response(
                200, json={"status": "0", "message": "No transactions found", "result": []}
            )
        return httpx.Response(200, json={"status": "1", "message": "OK", "result": rows})


async def _no_sleep(seconds: float) -> None:
    return None


@pytest.fixture
async def etherscan() -> FakeEtherscan:
    return FakeEtherscan()


@pytest.fixture
async def make_client(
    sessions: async_sessionmaker[AsyncSession], etherscan: FakeEtherscan
) -> AsyncIterator[Callable[..., httpx.AsyncClient]]:
    """Builds an API client backed by the fake Etherscan; tears jobs down afterwards."""
    async with sessions() as session:
        await sync_labels(
            session, [LabelRecord(address=SANCTIONED, label_type="sanctioned", source="test")]
        )
    created: list[ScoreJobs] = []
    http = httpx.AsyncClient(transport=httpx.MockTransport(etherscan))

    def make(*, timeout: float = 10, configured: bool = True) -> httpx.AsyncClient:
        client = EtherscanClient(
            "test-key",
            http_client=http,
            rate_limiter=TokenBucketRateLimiter(10_000),
            max_retries=2,
            sleep=_no_sleep,
        )
        cache = TransactionCache(sessions, client, ttl_seconds=3600) if configured else None
        jobs = ScoreJobs(
            sessions,
            cache,
            load_scoring_params(),
            job_timeout_seconds=timeout,
            max_concurrent=2,
            reuse_seconds=3600,
        )
        created.append(jobs)
        app = create_app(score_jobs=jobs)
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    yield make
    for jobs in created:
        await jobs.shutdown()
    await http.aclose()


async def _poll(api: httpx.AsyncClient, run_id: str) -> dict[str, Any]:
    for _ in range(200):
        body: dict[str, Any] = (await api.get(f"/api/v1/scores/{run_id}")).json()
        if body["status"] in ("done", "failed"):
            return body
        await asyncio.sleep(0.02)
    raise AssertionError("job did not finish")


async def test_submit_then_poll_returns_score_breakdown_and_flagged_graph(
    make_client: Callable[..., httpx.AsyncClient],
) -> None:
    async with make_client() as api:
        submitted = await api.post("/api/v1/scores", json={"address": TARGET})
        assert submitted.status_code == 202
        run = submitted.json()
        assert run["status"] in ("pending", "running")
        assert submitted.headers["location"] == f"/api/v1/scores/{run['id']}"

        done = await _poll(api, run["id"])

    assert done["status"] == "done"
    assert done["error"] is None
    result = done["result"]
    assert result["score"] == 100
    assert result["bucket"] == "severe"
    [item] = result["breakdown"]
    assert item["address"] == SANCTIONED
    assert item["hops"] == 1
    assert item["bottleneck_eq_wei"] == ETH
    roles = {n["address"]: n["role"] for n in result["graph"]["nodes"]}
    # Only the flagged path is returned: FUNDER isn't on it.
    assert roles == {TARGET: "target", SANCTIONED: "flagged"}
    assert result["stats"]["api_calls"] > 0
    assert done["params_hash"] == load_scoring_params().params_hash


async def test_resubmitting_reuses_the_finished_result_without_etherscan_calls(
    make_client: Callable[..., httpx.AsyncClient], etherscan: FakeEtherscan
) -> None:
    async with make_client() as api:
        first = (await api.post("/api/v1/scores", json={"address": TARGET})).json()
        await _poll(api, first["id"])
        calls = etherscan.requests

        again = await api.post("/api/v1/scores", json={"address": TARGET})

    assert again.status_code == 200
    assert again.json()["id"] == first["id"]
    assert again.json()["status"] == "done"
    assert etherscan.requests == calls


async def test_identical_requests_while_running_share_one_job(
    make_client: Callable[..., httpx.AsyncClient], etherscan: FakeEtherscan
) -> None:
    etherscan.gate = asyncio.Event()
    async with make_client() as api:
        first, second = await asyncio.gather(
            api.post("/api/v1/scores", json={"address": TARGET}),
            api.post("/api/v1/scores", json={"address": to_checksum_address(TARGET)}),
        )
        assert first.json()["id"] == second.json()["id"]
        assert second.status_code == 202
        etherscan.gate.set()
        assert (await _poll(api, first.json()["id"]))["status"] == "done"


@pytest.mark.parametrize(
    ("address", "message"),
    [
        ("0x123", "Not a valid Ethereum address"),
        ("0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAeD", "Invalid EIP-55 checksum"),
    ],
)
async def test_invalid_addresses_are_rejected_with_422(
    make_client: Callable[..., httpx.AsyncClient], address: str, message: str
) -> None:
    async with make_client() as api:
        response = await api.post("/api/v1/scores", json={"address": address})

    assert response.status_code == 422
    assert message in response.text


async def test_unknown_and_malformed_job_ids(
    make_client: Callable[..., httpx.AsyncClient],
) -> None:
    async with make_client() as api:
        missing = await api.get(f"/api/v1/scores/{uuid.uuid4()}")
        malformed = await api.get("/api/v1/scores/not-a-uuid")

    assert missing.status_code == 404
    assert missing.json() == {"detail": "Score not found"}
    assert malformed.status_code == 422


async def test_persistent_upstream_rate_limit_fails_the_job_clearly(
    make_client: Callable[..., httpx.AsyncClient], etherscan: FakeEtherscan
) -> None:
    etherscan.mode = "throttle"
    async with make_client() as api:
        run = (await api.post("/api/v1/scores", json={"address": TARGET})).json()
        done = await _poll(api, run["id"])

    assert done["status"] == "failed"
    assert done["result"] is None
    assert done["error"]["code"] == "upstream_rate_limited"


async def test_upstream_outage_fails_the_job_clearly(
    make_client: Callable[..., httpx.AsyncClient], etherscan: FakeEtherscan
) -> None:
    etherscan.mode = "down"
    async with make_client() as api:
        run = (await api.post("/api/v1/scores", json={"address": TARGET})).json()
        done = await _poll(api, run["id"])

    assert done["error"]["code"] == "upstream_unavailable"


async def test_slow_upstream_times_the_job_out(
    make_client: Callable[..., httpx.AsyncClient], etherscan: FakeEtherscan
) -> None:
    etherscan.gate = asyncio.Event()  # never released
    async with make_client(timeout=0.2) as api:
        run = (await api.post("/api/v1/scores", json={"address": TARGET})).json()
        done = await _poll(api, run["id"])

    assert done["status"] == "failed"
    assert done["error"]["code"] == "timeout"


async def test_a_failed_score_can_be_resubmitted(
    make_client: Callable[..., httpx.AsyncClient], etherscan: FakeEtherscan
) -> None:
    etherscan.mode = "down"
    async with make_client() as api:
        failed = (await api.post("/api/v1/scores", json={"address": TARGET})).json()
        await _poll(api, failed["id"])
        etherscan.mode = "ok"

        retried = await api.post("/api/v1/scores", json={"address": TARGET})
        done = await _poll(api, retried.json()["id"])

    assert retried.status_code == 202
    assert retried.json()["id"] != failed["id"]
    assert done["status"] == "done"


async def test_scoring_without_api_key_returns_503(
    make_client: Callable[..., httpx.AsyncClient],
) -> None:
    async with make_client(configured=False) as api:
        response = await api.post("/api/v1/scores", json={"address": TARGET})

    assert response.status_code == 503
    assert "ETHERSCAN_API_KEY" in response.json()["detail"]


async def test_history_lists_runs_newest_first(
    make_client: Callable[..., httpx.AsyncClient], etherscan: FakeEtherscan
) -> None:
    etherscan.mode = "down"
    async with make_client() as api:
        failed = (await api.post("/api/v1/scores", json={"address": TARGET})).json()
        await _poll(api, failed["id"])
        etherscan.mode = "ok"
        ok = (await api.post("/api/v1/scores", json={"address": TARGET})).json()
        await _poll(api, ok["id"])

        history = await api.get(f"/api/v1/wallets/{TARGET}/scores")
        invalid = await api.get("/api/v1/wallets/0x123/scores")

    assert [run["id"] for run in history.json()] == [ok["id"], failed["id"]]
    assert invalid.status_code == 422


async def test_startup_recovery_marks_interrupted_jobs_failed(
    sessions: async_sessionmaker[AsyncSession], make_client: Callable[..., httpx.AsyncClient]
) -> None:
    async with sessions() as session:
        stale = ScoreRun(address=TARGET, status="running", params_hash="old")
        session.add(stale)
        await session.commit()

    async with make_client() as api:
        jobs: ScoreJobs = api._transport.app.state.score_jobs  # type: ignore[attr-defined]
        assert await jobs.recover_interrupted() == 1
        body = (await api.get(f"/api/v1/scores/{stale.id}")).json()

    assert body["status"] == "failed"
    assert body["error"]["code"] == "interrupted"


async def test_openapi_documents_the_scoring_endpoints(
    make_client: Callable[..., httpx.AsyncClient],
) -> None:
    async with make_client() as api:
        spec = (await api.get("/openapi.json")).json()

    assert {"/api/v1/scores", "/api/v1/scores/{run_id}", "/api/v1/wallets/{address}/scores"} <= set(
        spec["paths"]
    )
    post = spec["paths"]["/api/v1/scores"]["post"]
    assert {"200", "202", "422", "503"} <= set(post["responses"])
    assert "ScoreRunOut" in spec["components"]["schemas"]
