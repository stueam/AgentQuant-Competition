"""Race schedule, frozen entries and official evaluation batches."""

from datetime import UTC, datetime, time, timedelta, timezone
from hashlib import sha256
from sqlite3 import Connection
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Request, Response
from pydantic import AwareDatetime, Field, model_validator

from .accounts import Authenticated, require_role, require_team
from .contracts import ArchiveSource, CatalogEntry, EvaluationResult, Run, RunSpec, Submission
from .demo import load_dataset
from .demo_catalog import CATALOG, DATASET_ID, OFFICIAL_DATASET_ID, RULES_VERSION, dataset_bytes
from .http import ApiError, Store
from .jobs import JobStore, enqueue, read_run
from .sdk import Contract

COMPETITION_ID = "track-2"
TIMEZONE = timezone(timedelta(hours=8))
router = APIRouter(prefix="/api/v1", tags=["competition"])

SCHEMA = """
CREATE TABLE IF NOT EXISTS competition_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    record TEXT NOT NULL,
    frozen_at TEXT
);
CREATE TABLE IF NOT EXISTS final_selections (
    team_id TEXT PRIMARY KEY REFERENCES teams(id),
    submission_id TEXT NOT NULL REFERENCES submissions(id),
    revision INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS freeze_records (
    team_id TEXT PRIMARY KEY REFERENCES teams(id),
    receipt TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS official_batches (
    id TEXT PRIMARY KEY,
    competition_id TEXT NOT NULL UNIQUE,
    dataset_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS official_runs (
    run_id TEXT PRIMARY KEY REFERENCES runs(id),
    batch_id TEXT NOT NULL REFERENCES official_batches(id),
    team_id TEXT NOT NULL REFERENCES teams(id),
    attempt INTEGER NOT NULL,
    UNIQUE (batch_id, team_id, attempt)
);
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY,
    actor_id TEXT NOT NULL REFERENCES users(id),
    action TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS runs_team_date ON runs(team_id, created_at);
"""


class CompetitionSettings(Contract):
    validation_deadline: AwareDatetime
    official_ready_at: AwareDatetime
    daily_validation_limit: Annotated[int, Field(strict=True, ge=1, le=1000)]
    quant_weight_bps: Annotated[int, Field(strict=True, ge=1, lt=10_000)]

    @model_validator(mode="after")
    def validate_dates(self):
        if self.official_ready_at < self.validation_deadline:
            raise ValueError("official_ready_at must be on or after validation_deadline")
        return self


class CompetitionState(Contract):
    id: str = COMPETITION_ID
    server_time: datetime
    phase: Literal["unconfigured", "validation", "awaiting_freeze", "frozen", "official_ready", "evaluating", "published"]
    rules_version: str = RULES_VERSION
    settings: CompetitionSettings | None


class SelectionInput(Contract):
    submission_id: UUID
    revision: Annotated[int, Field(strict=True, ge=0)]


class Selection(SelectionInput):
    team_id: UUID


class FreezeReceipt(Selection):
    frozen_at: datetime
    baseline_id: str
    rules_version: str
    seed: int = 0


class FreezeSummary(Contract):
    frozen_at: datetime
    entries: int


class OfficialRun(Contract):
    team_id: UUID
    attempt: int
    run: Run


class OfficialBatch(Contract):
    id: UUID
    dataset_id: str = OFFICIAL_DATASET_ID
    dataset_sha256: str
    created_at: datetime
    runs: list[OfficialRun]


class RetryInput(Contract):
    reason: Annotated[str, Field(min_length=1, max_length=500)]


def initialize(jobs: JobStore) -> None:
    with jobs.connect() as db:
        db.executescript(SCHEMA)


def audit(db: Connection, actor_id: UUID, action: str, resource: str, now: datetime, detail: str = "") -> None:
    db.execute(
        "INSERT INTO audit_events (actor_id, action, resource_id, detail, created_at) VALUES (?, ?, ?, ?, ?)",
        (str(actor_id), action, resource, detail, now.isoformat()),
    )


def ensure_competition(competition_id: str) -> None:
    if competition_id != COMPETITION_ID:
        raise ApiError(404, "COMPETITION_NOT_FOUND", "该赛事不存在")


def read_settings(db: Connection) -> tuple[CompetitionSettings | None, str | None]:
    row = db.execute("SELECT record, frozen_at FROM competition_settings WHERE id = 1").fetchone()
    return (CompetitionSettings.model_validate_json(row["record"]), row["frozen_at"]) if row else (None, None)


def check_validation(db: Connection, team_id: str, now: datetime, *, quota: bool = False) -> None:
    if not team_id:
        return
    settings, frozen = read_settings(db)
    if frozen or (settings and now >= settings.validation_deadline):
        raise ApiError(409, "VALIDATION_CLOSED", "验证提交已截止")
    if quota:
        day = now.astimezone(TIMEZONE).date()
        start = datetime.combine(day, time.min, TIMEZONE).astimezone(UTC)
        count = db.execute(
            "SELECT count(*) FROM runs WHERE team_id = ? AND created_at >= ? AND created_at < ?",
            (team_id, start.isoformat(), (start + timedelta(days=1)).isoformat()),
        ).fetchone()[0]
        limit = settings.daily_validation_limit if settings else 20
        if count >= limit:
            raise ApiError(429, "VALIDATION_LIMIT", "本队今日的验证额度已用完")


def competition_state(db: Connection, now: datetime) -> CompetitionState:
    settings, frozen = read_settings(db)
    if settings is None:
        phase = "unconfigured"
    elif now < settings.validation_deadline:
        phase = "validation"
    elif frozen is None:
        phase = "awaiting_freeze"
    elif now < settings.official_ready_at:
        phase = "frozen"
    elif db.execute("SELECT 1 FROM leaderboards WHERE published_at IS NOT NULL").fetchone():
        phase = "published"
    elif db.execute("SELECT 1 FROM official_batches").fetchone():
        phase = "evaluating"
    else:
        phase = "official_ready"
    return CompetitionState(server_time=now, phase=phase, settings=settings)


@router.get("/competitions/{competition_id}", operation_id="getCompetition")
def get_competition(competition_id: str, request: Request, jobs: Store) -> CompetitionState:
    ensure_competition(competition_id)
    with jobs.connect() as db:
        return competition_state(db, request.app.state.clock())


@router.put("/admin/competitions/{competition_id}", operation_id="configureCompetition")
def configure(competition_id: str, data: CompetitionSettings, current: Authenticated,
              request: Request, jobs: Store) -> CompetitionState:
    ensure_competition(competition_id)
    organizer = require_role(current, "organizer")
    with jobs.transaction() as db:
        now = request.app.state.clock()
        if data.validation_deadline <= now:
            raise ApiError(409, "DEADLINE_PASSED", "验证截止时间须晚于当前时间")
        saved = db.execute(
            "INSERT INTO competition_settings (id, record) VALUES (1, ?) ON CONFLICT(id) DO NOTHING",
            (data.model_dump_json(),),
        ).rowcount
        if not saved:
            raise ApiError(409, "SETTINGS_LOCKED", "赛事配置已生效")
        audit(db, organizer.id, "configure", competition_id, now, data.model_dump_json())
        return competition_state(db, now)


@router.get("/competitions/{competition_id}/datasets", operation_id="listDatasets")
def list_datasets(competition_id: str, current: Authenticated) -> tuple[CatalogEntry, ...]:
    ensure_competition(competition_id)
    require_team(current)
    return CATALOG.datasets


@router.get("/competitions/{competition_id}/datasets/{dataset_id}/download", operation_id="downloadDataset",
            response_class=Response, responses={200: {"content": {"application/json": {}}}})
def download_dataset(competition_id: str, dataset_id: str, current: Authenticated) -> Response:
    ensure_competition(competition_id)
    require_team(current)
    if dataset_id != DATASET_ID:
        raise ApiError(404, "DATASET_NOT_FOUND", "该数据集不存在")
    return Response(
        dataset_bytes(dataset_id), media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{DATASET_ID}.json"', "Cache-Control": "private, no-store"},
    )


@router.get("/competitions/{competition_id}/final-selection", operation_id="getFinalSelection")
def get_selection(competition_id: str, current: Authenticated, jobs: Store) -> Selection:
    ensure_competition(competition_id)
    team_id = require_team(current)
    with jobs.connect() as db:
        row = db.execute("SELECT * FROM final_selections WHERE team_id = ?", (team_id,)).fetchone()
    if row is None:
        raise ApiError(404, "SELECTION_NOT_FOUND", "本队尚未选择最终版本")
    return Selection.model_validate(dict(row))


@router.put("/competitions/{competition_id}/final-selection", operation_id="setFinalSelection")
def set_selection(competition_id: str, data: SelectionInput, current: Authenticated,
                  request: Request, jobs: Store) -> Selection:
    ensure_competition(competition_id)
    team_id = require_team(current)
    if not current.identity.captain:
        raise ApiError(403, "CAPTAIN_REQUIRED", "仅队长可选择最终版本")
    with jobs.transaction() as db:
        now = request.app.state.clock()
        check_validation(db, team_id, now)
        submission = db.execute(
            "SELECT record FROM submissions WHERE id = ? AND team_id = ?", (str(data.submission_id), team_id),
        ).fetchone()
        if submission is None:
            raise ApiError(404, "SUBMISSION_NOT_FOUND", "该提交不存在")
        if isinstance(Submission.model_validate_json(submission["record"]).source, ArchiveSource):
            raise ApiError(409, "STRATEGY_SMOKE_TEST_REQUIRED", "策略包尚未通过沙箱试运行")
        row = db.execute("SELECT revision FROM final_selections WHERE team_id = ?", (team_id,)).fetchone()
        revision = row["revision"] if row else 0
        if revision != data.revision:
            raise ApiError(409, "SELECTION_CONFLICT", "选择已更新，请重新读取版本")
        db.execute(
            """INSERT INTO final_selections VALUES (?, ?, ?)
               ON CONFLICT(team_id) DO UPDATE SET submission_id = excluded.submission_id, revision = excluded.revision""",
            (team_id, str(data.submission_id), revision + 1),
        )
        return Selection(team_id=team_id, submission_id=data.submission_id, revision=revision + 1)


@router.post("/admin/competitions/{competition_id}/freeze", operation_id="freezeCompetition")
def freeze(competition_id: str, current: Authenticated, request: Request, jobs: Store) -> FreezeSummary:
    ensure_competition(competition_id)
    organizer = require_role(current, "organizer")
    with jobs.transaction() as db:
        settings, frozen = read_settings(db)
        if frozen:
            count = db.execute("SELECT count(*) FROM freeze_records").fetchone()[0]
            return FreezeSummary(frozen_at=frozen, entries=count)
        now = request.app.state.clock()
        if not settings or now < settings.validation_deadline:
            raise ApiError(409, "FREEZE_NOT_DUE", "验证截止前无法封榜")
        rows = db.execute(
            """SELECT f.*, s.record FROM final_selections f JOIN submissions s ON f.submission_id = s.id
               ORDER BY f.team_id""",
        ).fetchall()
        for row in rows:
            submission = Submission.model_validate_json(row["record"])
            if isinstance(submission.source, ArchiveSource):
                raise ApiError(409, "STRATEGY_SMOKE_TEST_REQUIRED", "策略包尚未通过沙箱试运行")
            receipt = FreezeReceipt(
                team_id=row["team_id"], submission_id=submission.id, revision=row["revision"],
                frozen_at=now, baseline_id=submission.source.baseline_id, rules_version=RULES_VERSION,
            )
            db.execute("INSERT INTO freeze_records VALUES (?, ?)", (row["team_id"], receipt.model_dump_json()))
        db.execute("UPDATE competition_settings SET frozen_at = ? WHERE id = 1", (now.isoformat(),))
        audit(db, organizer.id, "freeze", competition_id, now, str(len(rows)))
        return FreezeSummary(frozen_at=now, entries=len(rows))


@router.get("/competitions/{competition_id}/freeze-receipt", operation_id="getFreezeReceipt")
def freeze_receipt(competition_id: str, current: Authenticated, jobs: Store) -> FreezeReceipt:
    ensure_competition(competition_id)
    team_id = require_team(current)
    with jobs.connect() as db:
        row = db.execute("SELECT receipt FROM freeze_records WHERE team_id = ?", (team_id,)).fetchone()
    if row is None:
        raise ApiError(404, "FREEZE_RECEIPT_NOT_FOUND", "本队暂无冻结回执")
    return FreezeReceipt.model_validate_json(row["receipt"])


def batch_response(db: Connection, batch) -> OfficialBatch:
    rows = db.execute(
        """SELECT r.*, o.attempt FROM official_runs o JOIN runs r ON r.id = o.run_id
           WHERE o.batch_id = ? ORDER BY o.team_id, o.attempt""",
        (batch["id"],),
    ).fetchall()
    return OfficialBatch(
        id=batch["id"], dataset_sha256=batch["dataset_sha256"], created_at=batch["created_at"],
        runs=[OfficialRun(team_id=row["team_id"], attempt=row["attempt"], run=read_run(row)) for row in rows],
    )


@router.post("/admin/official-runs", status_code=202, operation_id="createOfficialBatch")
def create_batch(current: Authenticated, request: Request, jobs: Store) -> OfficialBatch:
    organizer = require_role(current, "organizer")
    with jobs.transaction() as db:
        previous = db.execute("SELECT * FROM official_batches WHERE competition_id = ?", (COMPETITION_ID,)).fetchone()
        if previous:
            return batch_response(db, previous)
        settings, frozen = read_settings(db)
        now = request.app.state.clock()
        if not frozen or settings is None or now < settings.official_ready_at:
            raise ApiError(409, "OFFICIAL_NOT_READY", "封榜和正式数据窗口完成后才能创建正式批次")
        receipts = db.execute("SELECT receipt FROM freeze_records ORDER BY team_id").fetchall()
        if not receipts:
            raise ApiError(409, "NO_FROZEN_ENTRIES", "没有已冻结的参赛提交")
        dataset_hash = sha256(dataset_bytes(OFFICIAL_DATASET_ID)).hexdigest()
        load_dataset(OFFICIAL_DATASET_ID, dataset_hash)
        batch_id = str(uuid4())
        batch = db.execute(
            "INSERT INTO official_batches VALUES (?, ?, ?, ?) RETURNING *",
            (batch_id, COMPETITION_ID, dataset_hash, now.isoformat()),
        ).fetchone()
        for row in receipts:
            receipt = FreezeReceipt.model_validate_json(row["receipt"])
            spec = RunSpec(
                run_id=uuid4(), submission_id=receipt.submission_id, baseline_id=receipt.baseline_id,
                dataset_id=OFFICIAL_DATASET_ID, dataset_sha256=dataset_hash, rules_version=receipt.rules_version,
                seed=receipt.seed, kind="official",
            )
            run = enqueue(db, spec, str(receipt.team_id), str(uuid4()), now)
            db.execute("INSERT INTO official_runs VALUES (?, ?, ?, 1)", (str(run.id), batch_id, str(receipt.team_id)))
        audit(db, organizer.id, "start_official_batch", batch_id, now, dataset_hash)
        return batch_response(db, batch)


@router.get("/admin/official-runs/{batch_id}", operation_id="getOfficialBatch")
def get_batch(batch_id: UUID, current: Authenticated, jobs: Store) -> OfficialBatch:
    require_role(current, "organizer")
    with jobs.connect() as db:
        batch = db.execute("SELECT * FROM official_batches WHERE id = ?", (str(batch_id),)).fetchone()
        if batch is None:
            raise ApiError(404, "BATCH_NOT_FOUND", "该正式批次不存在")
        return batch_response(db, batch)


@router.get("/admin/runs/{run_id}/result", operation_id="getOfficialResult")
def official_result(run_id: UUID, current: Authenticated, jobs: Store) -> EvaluationResult:
    require_role(current, "organizer")
    with jobs.connect() as db:
        row = db.execute(
            "SELECT r.result FROM runs r JOIN official_runs o ON o.run_id = r.id WHERE r.id = ?", (str(run_id),),
        ).fetchone()
    if row is None:
        raise ApiError(404, "RUN_NOT_FOUND", "该正式任务不存在")
    if row["result"] is None:
        raise ApiError(409, "RESULT_UNAVAILABLE", "该任务暂无成功结果")
    return EvaluationResult.model_validate_json(row["result"])


@router.post("/admin/runs/{run_id}/retry", status_code=202, operation_id="retryOfficialRun")
def retry(run_id: UUID, data: RetryInput, current: Authenticated, request: Request, jobs: Store) -> OfficialRun:
    organizer = require_role(current, "organizer")
    with jobs.transaction() as db:
        previous = db.execute(
            """SELECT o.*, r.spec, r.status FROM official_runs o JOIN runs r ON r.id = o.run_id
               WHERE o.run_id = ?""",
            (str(run_id),),
        ).fetchone()
        if previous is None:
            raise ApiError(404, "RUN_NOT_FOUND", "该正式任务不存在")
        attempt = previous["attempt"] + 1
        existing = db.execute(
            """SELECT r.* FROM official_runs o JOIN runs r ON r.id = o.run_id
               WHERE o.batch_id = ? AND o.team_id = ? AND o.attempt = ?""",
            (previous["batch_id"], previous["team_id"], attempt),
        ).fetchone()
        if existing:
            return OfficialRun(team_id=previous["team_id"], attempt=attempt, run=read_run(existing))
        if previous["status"] != "FAILED":
            raise ApiError(409, "RETRY_NOT_ALLOWED", "仅失败的正式任务可重跑")
        if db.execute("SELECT 1 FROM leaderboards WHERE published_at IS NOT NULL").fetchone():
            raise ApiError(409, "LEADERBOARD_LOCKED", "榜单已发布，正式评测已锁定")
        spec = RunSpec.model_validate_json(previous["spec"]).model_copy(update={"run_id": uuid4()})
        now = request.app.state.clock()
        run = enqueue(db, spec, previous["team_id"], str(uuid4()), now)
        db.execute(
            "INSERT INTO official_runs VALUES (?, ?, ?, ?)",
            (str(run.id), previous["batch_id"], previous["team_id"], attempt),
        )
        audit(db, organizer.id, "retry_official_run", str(run.id), now, data.reason)
        return OfficialRun(team_id=previous["team_id"], attempt=attempt, run=run)
