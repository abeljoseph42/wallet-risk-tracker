"""Scoring endpoints: submit a wallet, poll the job, list a wallet's history."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response, status

from app.api.schemas import ErrorDetail, ScoreRequest, ScoreRunOut
from app.core.addresses import InvalidAddressError
from app.services.score_jobs import ScoreJobs, ScoringUnavailableError

router = APIRouter(prefix="/api/v1", tags=["scores"])


def get_score_jobs(request: Request) -> ScoreJobs:
    jobs: ScoreJobs = request.app.state.score_jobs
    return jobs


Jobs = Annotated[ScoreJobs, Depends(get_score_jobs)]


@router.post(
    "/scores",
    response_model=ScoreRunOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Score a wallet",
    description=(
        "Starts a scoring job and returns it with status `pending` (202). Poll "
        "`GET /api/v1/scores/{id}` until it is `done` or `failed`. If a recent finished "
        "result or an identical job in progress already exists for this address and "
        "parameters, it is returned instead (200 when finished, 202 when in progress)."
    ),
    responses={
        200: {"model": ScoreRunOut, "description": "A recent finished result was reused."},
        422: {"description": "Invalid address (format or EIP-55 checksum)."},
        503: {"model": ErrorDetail, "description": "Scoring is not configured."},
    },
)
async def submit_score(body: ScoreRequest, jobs: Jobs, response: Response) -> ScoreRunOut:
    try:
        run, _started = await jobs.submit(body.address)
    except ScoringUnavailableError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    response.headers["Location"] = f"/api/v1/scores/{run.id}"
    if run.status == "done":
        response.status_code = status.HTTP_200_OK
    return ScoreRunOut.from_run(run)


@router.get(
    "/scores/{run_id}",
    response_model=ScoreRunOut,
    summary="Get a scoring job",
    description="Returns the job's status, and its result or error once finished.",
    responses={404: {"model": ErrorDetail, "description": "No such job."}},
)
async def get_score(run_id: uuid.UUID, jobs: Jobs) -> ScoreRunOut:
    run = await jobs.get(run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Score not found")
    return ScoreRunOut.from_run(run)


@router.get(
    "/wallets/{address}/scores",
    response_model=list[ScoreRunOut],
    summary="List a wallet's scores",
    description="Most recent first, across all parameter versions.",
    responses={422: {"model": ErrorDetail, "description": "Invalid address."}},
)
async def list_scores(
    address: Annotated[str, Path(description="Ethereum address.")],
    jobs: Jobs,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[ScoreRunOut]:
    try:
        runs = await jobs.history(address, limit)
    except InvalidAddressError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    return [ScoreRunOut.from_run(run) for run in runs]
