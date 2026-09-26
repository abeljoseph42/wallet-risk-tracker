"""Scoring jobs: run graph building + scoring in the background, tracked in score_runs.

Jobs are asyncio tasks in the API process (no queue infrastructure). The trade-off: a
restart loses in-flight jobs, so `recover_interrupted` marks them failed on startup and
clients can resubmit. At most `max_concurrent` jobs run at once because they all share
one Etherscan rate limit; the rest wait as `pending`.
"""

import asyncio
import datetime
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clients.etherscan import EtherscanError
from app.config import ScoringParams
from app.core.addresses import normalize_address
from app.db.models import ScoreRun
from app.services.graph import GraphBuilder, TransferLookup
from app.services.labels import load_label_index, load_severity_overrides
from app.services.scoring import breakdown_json, flagged_subgraph, score_graph
from app.services.valuation import make_valuer

logger = logging.getLogger(__name__)

_ERROR_CODES = {
    "rate_limited": "upstream_rate_limited",
    "unavailable": "upstream_unavailable",
    "api": "upstream_error",
    "config": "not_configured",
}


@dataclass(frozen=True)
class _Outcome:
    score: float
    bucket: str
    breakdown: list[dict[str, object]]
    graph: dict[str, object]
    stats: dict[str, object]


class ScoringUnavailableError(Exception):
    """Scoring can't run in this process (no Etherscan API key configured)."""


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


class ScoreJobs:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        lookup: TransferLookup | None,
        params: ScoringParams,
        *,
        job_timeout_seconds: float,
        max_concurrent: int,
        reuse_seconds: int,
        now: Callable[[], datetime.datetime] = _utcnow,
    ) -> None:
        self._sessions = sessions
        self._lookup = lookup
        self._params = params
        self._timeout = job_timeout_seconds
        self._reuse = datetime.timedelta(seconds=reuse_seconds)
        self._now = now
        self._semaphore = asyncio.Semaphore(max_concurrent)
        # Serializes the check-then-insert in submit(), so two identical requests
        # arriving together start one job, not two.
        self._submit_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def params(self) -> ScoringParams:
        return self._params

    async def submit(self, address: str) -> tuple[ScoreRun, bool]:
        """Return (run, started). Reuses a fresh result or an identical job in progress."""
        if self._lookup is None:
            raise ScoringUnavailableError("ETHERSCAN_API_KEY is not configured")
        address = normalize_address(address)
        params_hash = self._params.params_hash

        async with self._submit_lock, self._sessions() as session:
            latest = await session.scalar(
                select(ScoreRun)
                .where(ScoreRun.address == address, ScoreRun.params_hash == params_hash)
                .order_by(ScoreRun.created_at.desc())
                .limit(1)
            )
            if latest is not None and self._reusable(latest):
                return latest, False
            run = ScoreRun(address=address, status="pending", params_hash=params_hash)
            session.add(run)
            await session.commit()
            await session.refresh(run)

        task = asyncio.create_task(self._run(run.id, address), name=f"score-{run.id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return run, True

    def _reusable(self, run: ScoreRun) -> bool:
        if run.status in ("pending", "running"):
            return True
        return (
            run.status == "done"
            and run.finished_at is not None
            and self._now() - run.finished_at < self._reuse
        )

    async def get(self, run_id: uuid.UUID) -> ScoreRun | None:
        async with self._sessions() as session:
            return await session.get(ScoreRun, run_id)

    async def history(self, address: str, limit: int) -> list[ScoreRun]:
        address = normalize_address(address)
        async with self._sessions() as session:
            rows = await session.scalars(
                select(ScoreRun)
                .where(ScoreRun.address == address)
                .order_by(ScoreRun.created_at.desc())
                .limit(limit)
            )
            return list(rows)

    async def recover_interrupted(self) -> int:
        """Mark jobs left pending/running by a previous process as failed."""
        async with self._sessions() as session:
            result = await session.execute(
                update(ScoreRun)
                .where(ScoreRun.status.in_(("pending", "running")))
                .values(
                    status="failed",
                    error_code="interrupted",
                    error_message="The server restarted while this job was in progress.",
                    finished_at=self._now(),
                )
                .returning(ScoreRun.id)
            )
            await session.commit()
            return len(result.all())

    async def wait_idle(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _run(self, run_id: uuid.UUID, address: str) -> None:
        async with self._semaphore:
            await self._update(run_id, status="running", started_at=self._now())
            started = time.perf_counter()
            try:
                async with asyncio.timeout(self._timeout):
                    outcome = await self._score(address)
            except TimeoutError:
                await self._fail(
                    run_id, "timeout", f"Scoring took longer than {self._timeout:g} seconds."
                )
            except EtherscanError as exc:
                await self._fail(run_id, _ERROR_CODES[exc.kind], str(exc))
            except Exception:
                logger.exception("Scoring job %s for %s failed", run_id, address)
                await self._fail(run_id, "internal", "Unexpected error while scoring.")
            else:
                duration_ms = round((time.perf_counter() - started) * 1000)
                await self._update(
                    run_id,
                    status="done",
                    finished_at=self._now(),
                    score=outcome.score,
                    bucket=outcome.bucket,
                    breakdown_json=outcome.breakdown,
                    graph_json=outcome.graph,
                    stats_json={**outcome.stats, "duration_ms": duration_ms},
                )

    async def _score(self, address: str) -> _Outcome:
        assert self._lookup is not None
        async with self._sessions() as session:
            labels = await load_label_index(session)
            overrides = await load_severity_overrides(session)
        builder = GraphBuilder(
            self._lookup, labels, self._params.graph, make_valuer(self._params.valuation)
        )
        graph = await builder.build(address)
        result = score_graph(graph, self._params, overrides)
        return _Outcome(
            score=result.score,
            bucket=result.bucket,
            breakdown=breakdown_json(result),
            graph=dict(flagged_subgraph(graph, result)),
            stats={
                "nodes": graph.graph.number_of_nodes(),
                "edges": graph.graph.number_of_edges(),
                **asdict(graph.stats),
            },
        )

    async def _fail(self, run_id: uuid.UUID, code: str, message: str) -> None:
        logger.warning("Scoring job %s failed: %s: %s", run_id, code, message)
        await self._update(
            run_id,
            status="failed",
            error_code=code,
            error_message=message,
            finished_at=self._now(),
        )

    async def _update(self, run_id: uuid.UUID, **values: object) -> None:
        async with self._sessions() as session:
            await session.execute(update(ScoreRun).where(ScoreRun.id == run_id).values(**values))
            await session.commit()
