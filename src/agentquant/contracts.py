"""Versioned messages shared across API, evaluator, and worker."""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StringConstraints

from .sdk import Contract

Identifier = Annotated[str, StringConstraints(min_length=1, max_length=64)]
Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
RunStatus = Literal["QUEUED", "RUNNING", "SUCCEEDED", "FAILED"]


class BaselineSource(Contract):
    kind: Literal["baseline"]
    baseline_id: Identifier


class ArchiveSource(Contract):
    kind: Literal["archive"]
    upload_id: UUID


class CreateSubmission(Contract):
    name: Title
    source: Annotated[BaselineSource | ArchiveSource, Field(discriminator="kind")]


class Submission(CreateSubmission):
    id: UUID
    created_at: datetime
    version: int | None = None
    artifact_sha256: str | None = None


class CreateRun(Contract):
    submission_id: UUID
    dataset_id: Identifier


class RunSpec(Contract):
    run_id: UUID
    submission_id: UUID
    baseline_id: Identifier
    dataset_id: Identifier
    rules_version: Identifier
    seed: int = 0
    kind: Literal["validation", "official"] = "validation"
    dataset_sha256: str | None = None


class Metrics(Contract):
    initial_cash: Decimal
    final_equity: Decimal
    net_return: Decimal
    max_drawdown: Decimal
    total_fees: Decimal


class EquityPoint(Contract):
    trading_date: date
    equity: Decimal


class Trade(Contract):
    trading_date: date
    asset_id: str
    side: Literal["BUY", "SELL"]
    shares: int
    price: Decimal
    fee: Decimal


class TradePage(Contract):
    offset: int
    limit: int
    total: int
    items: tuple[Trade, ...]


class EvaluationResult(Contract):
    metrics: Metrics
    equity: tuple[EquityPoint, ...]
    trades: tuple[Trade, ...]


class RunError(Contract):
    code: str
    message: str


class ErrorResponse(RunError):
    request_id: UUID


class Run(Contract):
    id: UUID
    submission_id: UUID
    dataset_id: Identifier
    rules_version: Identifier
    status: RunStatus
    created_at: datetime
    error: RunError | None = None
    kind: Literal["validation", "official"] = "validation"


class CatalogEntry(Contract):
    id: Identifier
    name: str


class Catalog(Contract):
    mode: Literal["demo"] = "demo"
    datasets: tuple[CatalogEntry, ...]
    baselines: tuple[CatalogEntry, ...]
