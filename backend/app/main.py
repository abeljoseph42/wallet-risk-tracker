"""FastAPI application entrypoint."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.health import router as health_router
from app.api.scores import router as scores_router
from app.clients.etherscan import client_from_settings
from app.config import get_settings, load_scoring_params
from app.db.session import get_sessionmaker
from app.services.cache import TransactionCache
from app.services.score_jobs import ScoreJobs

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    if getattr(app.state, "score_jobs", None) is not None:
        # Provided by the caller (tests); nothing to build or tear down.
        yield
        return

    settings = get_settings()
    sessions = get_sessionmaker()
    async with httpx.AsyncClient(timeout=30) as http:
        cache = None
        if settings.etherscan_api_key:
            # One client per process: every job shares its rate limiter.
            client = client_from_settings(settings, http)
            cache = TransactionCache(sessions, client, ttl_seconds=settings.cache_ttl_seconds)
        else:
            logger.warning("ETHERSCAN_API_KEY is not set; scoring endpoints will return 503")
        jobs = ScoreJobs(
            sessions,
            cache,
            load_scoring_params(),
            job_timeout_seconds=settings.score_job_timeout_seconds,
            max_concurrent=settings.score_max_concurrent_jobs,
            reuse_seconds=settings.score_reuse_seconds,
        )
        interrupted = await jobs.recover_interrupted()
        if interrupted:
            logger.warning("Marked %d interrupted scoring jobs as failed", interrupted)
        app.state.score_jobs = jobs
        try:
            yield
        finally:
            await jobs.shutdown()


def create_app(score_jobs: ScoreJobs | None = None) -> FastAPI:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)

    app = FastAPI(
        title="Wallet Risk Tracker",
        version="0.1.0",
        description=(
            "Scores Ethereum wallets 0-100 by transaction-graph proximity to sanctioned, "
            "malicious and mixer addresses. Scoring runs as a background job: submit with "
            "`POST /api/v1/scores`, then poll `GET /api/v1/scores/{id}`."
        ),
        lifespan=_lifespan,
    )
    app.state.score_jobs = score_jobs
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )
    app.include_router(health_router)
    app.include_router(scores_router)
    return app


app = create_app()
