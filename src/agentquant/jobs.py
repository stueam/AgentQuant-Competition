"""SQLite persistence for submissions and the shared API/Worker queue."""

import os
import sqlite3
from collections.abc import Callable
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import time
from uuid import UUID, uuid4

from .contracts import ArchiveSource, CreateRun, CreateSubmission, EvaluationResult, Run, RunError, RunSpec, Submission

SCHEMA = """
CREATE TABLE IF NOT EXISTS submissions (
    id TEXT PRIMARY KEY,
    record TEXT NOT NULL,
    team_id TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    submission_id TEXT NOT NULL REFERENCES submissions(id),
    idempotency_key TEXT NOT NULL,
    team_id TEXT NOT NULL DEFAULT '',
    request TEXT NOT NULL,
    spec TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    lease_until REAL,
    lease_token TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    result TEXT,
    error TEXT,
    UNIQUE (team_id, idempotency_key)
);
"""


class IdempotencyConflict(Exception):
    pass


class ArchiveUnavailable(Exception):
    pass


class ArchiveExecutionUnavailable(Exception):
    pass


@dataclass(frozen=True)
class Claim:
    spec: RunSpec
    token: str


class JobStore:
    def __init__(self, path: Path, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.path = path
        self.clock = clock
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as db:
            for statement in SCHEMA.strip().rstrip(";").split(";"):
                db.execute(statement)
            self.migrate_demo(db)
            db.execute("CREATE INDEX IF NOT EXISTS submissions_team ON submissions(team_id)")
            db.execute("CREATE INDEX IF NOT EXISTS runs_queue ON runs(status, created_at)")

    @staticmethod
    def migrate_demo(db: sqlite3.Connection) -> None:
        if "team_id" in {row["name"] for row in db.execute("PRAGMA table_info(runs)")}:
            return
        db.execute("ALTER TABLE submissions ADD COLUMN team_id TEXT NOT NULL DEFAULT ''")
        db.execute("ALTER TABLE runs RENAME TO demo_runs")
        db.execute(SCHEMA[SCHEMA.index("CREATE TABLE IF NOT EXISTS runs"):])
        columns = "id, submission_id, idempotency_key, request, spec, status, created_at, lease_until, lease_token, attempts, result, error"
        db.execute(f"INSERT INTO runs ({columns}) SELECT {columns} FROM demo_runs")
        db.execute("DROP TABLE demo_runs")

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            yield db

    @contextmanager
    def transaction(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    def create_submission(
        self, data: CreateSubmission, team_id: str = "", *,
        authorize: Callable[[sqlite3.Connection, datetime], None] | None = None,
    ) -> Submission:
        with self.transaction() as db:
            now = self.clock()
            if authorize is not None:
                authorize(db, now)
            checksum = None
            if isinstance(data.source, ArchiveSource):
                if not team_id:
                    raise ArchiveUnavailable
                upload = db.execute(
                    """SELECT expected_sha256 FROM uploads
                       WHERE id = ? AND team_id = ? AND completed_at IS NOT NULL""",
                    (str(data.source.upload_id), team_id),
                ).fetchone()
                if upload is None:
                    raise ArchiveUnavailable
                checksum = upload["expected_sha256"]
            version = None
            if team_id:
                version = db.execute("SELECT count(*) + 1 FROM submissions WHERE team_id = ?", (team_id,)).fetchone()[0]
            item = Submission(
                **data.model_dump(), id=uuid4(), created_at=now, version=version, artifact_sha256=checksum,
            )
            db.execute(
                "INSERT INTO submissions (id, record, team_id) VALUES (?, ?, ?)",
                (str(item.id), item.model_dump_json(), team_id),
            )
        return item

    def list_submissions(self, team_id: str = "", *, offset: int = 0, limit: int | None = None) -> list[Submission]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT record FROM submissions WHERE team_id = ? ORDER BY rowid DESC LIMIT ? OFFSET ?",
                (team_id, -1 if limit is None else limit, offset),
            ).fetchall()
        return [Submission.model_validate_json(row["record"]) for row in rows]

    def create_run(
        self, data: CreateRun, key: str, rules_version: str, team_id: str = "", *,
        authorize: Callable[[sqlite3.Connection, datetime], None] | None = None,
    ) -> Run | None:
        with self.transaction() as db:
            previous = db.execute(
                "SELECT * FROM runs WHERE team_id = ? AND idempotency_key = ?", (team_id, key),
            ).fetchone()
            if previous:
                if CreateRun.model_validate_json(previous["request"]) != data:
                    raise IdempotencyConflict
                return read_run(previous)
            row = db.execute(
                "SELECT record FROM submissions WHERE id = ? AND team_id = ?",
                (str(data.submission_id), team_id),
            ).fetchone()
            if row is None:
                return None
            submission = Submission.model_validate_json(row["record"])
            if isinstance(submission.source, ArchiveSource):
                raise ArchiveExecutionUnavailable
            now = self.clock()
            if authorize is not None:
                authorize(db, now)
            spec = RunSpec(
                run_id=uuid4(), submission_id=submission.id, dataset_id=data.dataset_id,
                baseline_id=submission.source.baseline_id, rules_version=rules_version,
            )
            return enqueue(db, spec, team_id, key, now)

    def get_run(self, run_id: UUID, team_id: str = "") -> tuple[Run, EvaluationResult | None] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id = ? AND team_id = ?", (str(run_id), team_id)).fetchone()
        if row is None:
            return None
        run = read_run(row)
        if run.kind != "validation":
            return None
        result = EvaluationResult.model_validate_json(row["result"]) if row["result"] else None
        return run, result

    def claim(self, lease_seconds: float = 120) -> Claim | None:
        token = str(uuid4())
        expired = RunError(code="LEASE_EXPIRED", message="评测进程中断，重试次数已用完")
        with self.transaction() as db:
            now = time()
            db.execute(
                """UPDATE runs SET status = 'FAILED', error = ?, lease_token = NULL, lease_until = NULL
                   WHERE status = 'RUNNING' AND lease_until <= ? AND attempts >= 2""",
                (expired.model_dump_json(), now),
            )
            row = db.execute(
                """UPDATE runs SET status = 'RUNNING', lease_token = ?, lease_until = ?, attempts = attempts + 1
                   WHERE id = (
                       SELECT id FROM runs WHERE status = 'QUEUED'
                           OR (status = 'RUNNING' AND lease_until <= ? AND attempts < 2)
                       ORDER BY created_at, id LIMIT 1
                   ) RETURNING spec""",
                (token, now + lease_seconds, now),
            ).fetchone()
        return Claim(RunSpec.model_validate_json(row["spec"]), token) if row else None

    def finish(self, claim: Claim, outcome: EvaluationResult | RunError) -> bool:
        result = isinstance(outcome, EvaluationResult)
        with self.transaction() as db:
            cursor = db.execute(
                """UPDATE runs SET status = ?, result = ?, error = ?, lease_token = NULL, lease_until = NULL
                   WHERE id = ? AND status = 'RUNNING' AND lease_token = ? AND lease_until > ?""",
                ("SUCCEEDED" if result else "FAILED",
                 outcome.model_dump_json() if result else None,
                 None if result else outcome.model_dump_json(),
                 str(claim.spec.run_id), claim.token, time()),
            )
            return cursor.rowcount == 1


def enqueue(db: sqlite3.Connection, spec: RunSpec, team_id: str, key: str, now: datetime) -> Run:
    now = now.astimezone(UTC)
    data = CreateRun(submission_id=spec.submission_id, dataset_id=spec.dataset_id)
    db.execute(
        """INSERT INTO runs (id, submission_id, idempotency_key, request, spec, status, created_at, team_id)
           VALUES (?, ?, ?, ?, ?, 'QUEUED', ?, ?)""",
        (str(spec.run_id), str(spec.submission_id), key, data.model_dump_json(),
         spec.model_dump_json(), now.isoformat(), team_id),
    )
    return Run(
        id=spec.run_id, submission_id=spec.submission_id, dataset_id=spec.dataset_id,
        rules_version=spec.rules_version, status="QUEUED", created_at=now, kind=spec.kind,
    )


def read_run(row: sqlite3.Row) -> Run:
    spec = RunSpec.model_validate_json(row["spec"])
    return Run(
        id=spec.run_id, submission_id=spec.submission_id, dataset_id=spec.dataset_id,
        rules_version=spec.rules_version, status=row["status"], created_at=row["created_at"], kind=spec.kind,
        error=RunError.model_validate_json(row["error"]) if row["error"] else None,
    )


def local_database() -> Path:
    return Path(os.environ.get("AGENTQUANT_DB", ".agentquant/local.sqlite3"))
