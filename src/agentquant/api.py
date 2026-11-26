"""HTTP entry point for AgentQuant."""

import json
import os
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from . import accounts, competition, reviews, uploads
from .contracts import (
    BaselineSource,
    Catalog,
    CreateRun,
    CreateSubmission,
    EquityPoint,
    ErrorResponse,
    EvaluationResult,
    Metrics,
    Run,
    Submission,
    TradePage,
)
from .demo_catalog import CATALOG, DATASET_ID, RULES_VERSION
from .http import ApiError, IdempotencyKey, Store, request_id
from .jobs import ArchiveExecutionUnavailable, ArchiveUnavailable, IdempotencyConflict, JobStore, local_database


def create_app(
    database: Path | None = None, *, allow_demo: bool | None = None, secure_cookie: bool | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.jobs = JobStore(database if database is not None else local_database(), clock)
        accounts.initialize(app.state.jobs)
        competition.initialize(app.state.jobs)
        reviews.initialize(app.state.jobs)
        uploads.initialize(app.state.jobs)
        app.state.clock = clock
        app.state.allow_demo = allow_demo if allow_demo is not None else os.getenv("AGENTQUANT_ALLOW_DEMO", "1") == "1"
        app.state.secure_cookie = (
            secure_cookie if secure_cookie is not None else os.getenv("AGENTQUANT_SECURE_COOKIE", "0") == "1"
        )
        yield

    auth_errors = {401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}}
    app = FastAPI(title="AgentQuant", version="0.1.0", lifespan=lifespan)
    app.include_router(accounts.router, responses={**auth_errors, 404: {"model": ErrorResponse},
                                                   409: {"model": ErrorResponse}})
    app.include_router(competition.router, responses={**auth_errors, 404: {"model": ErrorResponse},
                                                      409: {"model": ErrorResponse}})
    app.include_router(reviews.router, responses={**auth_errors, 404: {"model": ErrorResponse},
                                                  409: {"model": ErrorResponse}})
    app.include_router(uploads.router, responses={**auth_errors, 404: {"model": ErrorResponse},
                                                  409: {"model": ErrorResponse}})
    prefix = "/api/v1"
    not_found = {**auth_errors, 404: {"model": ErrorResponse}}
    result_errors = {**not_found, 409: {"model": ErrorResponse}}

    @app.exception_handler(ApiError)
    async def handle_error(request: Request, exc: ApiError) -> JSONResponse:
        body = ErrorResponse(code=exc.code, message=exc.message, request_id=request_id(request))
        return JSONResponse(
            status_code=exc.status, content=body.model_dump(mode="json"),
            headers={"X-Request-ID": str(body.request_id)},
        )

    @app.get(f"{prefix}/catalog", operation_id="getCatalog")
    def catalog() -> Catalog:
        return CATALOG

    @app.get(f"{prefix}/submissions", operation_id="listSubmissions", responses=auth_errors)
    def submissions(
        jobs: Store, team: accounts.ValidationTeam,
        offset: Annotated[int, Query(ge=0, le=2**31 - 1)] = 0,
        limit: Annotated[int | None, Query(ge=1, le=100)] = None,
    ) -> list[Submission]:
        return jobs.list_submissions(team, offset=offset, limit=limit)

    @app.post(f"{prefix}/submissions", status_code=201, operation_id="createSubmission", responses={**not_found, 409: {"model": ErrorResponse}})
    def create_submission(
        data: CreateSubmission, jobs: Store, team: accounts.ValidationTeam,
    ) -> Submission:
        if isinstance(data.source, BaselineSource) and data.source.baseline_id not in {
            entry.id for entry in CATALOG.baselines
        }:
            raise ApiError(404, "BASELINE_NOT_FOUND", "该示例策略不存在")
        try:
            return jobs.create_submission(
                data, team, authorize=lambda db, now: competition.check_validation(db, team, now),
            )
        except ArchiveUnavailable:
            raise ApiError(404, "ARCHIVE_NOT_FOUND", "该策略包不存在或尚未完成校验") from None

    @app.post(f"{prefix}/runs", status_code=202, operation_id="createRun",
              responses={**not_found, 409: {"model": ErrorResponse}, 429: {"model": ErrorResponse}})
    def create_run(
        data: CreateRun, jobs: Store, team: accounts.ValidationTeam,
        key: IdempotencyKey,
    ) -> Run:
        if data.dataset_id != DATASET_ID:
            raise ApiError(404, "DATASET_NOT_FOUND", "该样例数据集不存在")
        try:
            run = jobs.create_run(
                data, key, RULES_VERSION, team,
                authorize=lambda db, now: competition.check_validation(db, team, now, quota=True),
            )
        except IdempotencyConflict:
            raise ApiError(409, "IDEMPOTENCY_CONFLICT", "相同请求标识已用于不同的请求") from None
        except ArchiveExecutionUnavailable:
            raise ApiError(409, "STRATEGY_SMOKE_TEST_REQUIRED", "策略包尚未通过沙箱试运行") from None
        if run is None:
            raise ApiError(404, "SUBMISSION_NOT_FOUND", "该提交不存在")
        return run

    @app.get(f"{prefix}/runs/{{run_id}}", operation_id="getRun", responses=not_found)
    def run_status(run_id: UUID, jobs: Store, team: accounts.ValidationTeam) -> Run:
        entry = jobs.get_run(run_id, team)
        if entry is None:
            raise ApiError(404, "RUN_NOT_FOUND", "该验证任务不存在")
        return entry[0]

    @app.get(f"{prefix}/runs/{{run_id}}/result", operation_id="getResult", responses=result_errors)
    def run_result(run_id: UUID, jobs: Store, team: accounts.ValidationTeam) -> EvaluationResult:
        entry = jobs.get_run(run_id, team)
        if entry is None:
            raise ApiError(404, "RUN_NOT_FOUND", "该验证任务不存在")
        if entry[1] is None:
            raise ApiError(409, "RESULT_UNAVAILABLE", "该任务暂无成功结果")
        return entry[1]

    @app.get(f"{prefix}/runs/{{run_id}}/metrics", operation_id="getRunMetrics", responses=result_errors)
    def run_metrics(run_id: UUID, jobs: Store, team: accounts.ValidationTeam) -> Metrics:
        return run_result(run_id, jobs, team).metrics

    @app.get(f"{prefix}/runs/{{run_id}}/equity", operation_id="getRunEquity", responses=result_errors)
    def run_equity(run_id: UUID, jobs: Store, team: accounts.ValidationTeam) -> tuple[EquityPoint, ...]:
        return run_result(run_id, jobs, team).equity

    @app.get(f"{prefix}/runs/{{run_id}}/trades", operation_id="listRunTrades", responses=result_errors)
    def run_trades(
        run_id: UUID, jobs: Store, team: accounts.ValidationTeam,
        offset: Annotated[int, Query(ge=0, le=2**31 - 1)] = 0,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> TradePage:
        trades = run_result(run_id, jobs, team).trades
        return TradePage(offset=offset, limit=limit, total=len(trades), items=trades[offset:offset + limit])

    return app


app = create_app()


def export_openapi(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(app.openapi(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Export the current HTTP API contract.")
    parser.add_argument("output", type=Path)
    export_openapi(parser.parse_args().output)
