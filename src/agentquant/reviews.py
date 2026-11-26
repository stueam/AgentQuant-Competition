"""Report versions, assigned reviews and reproducible leaderboard publication."""

from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
from sqlite3 import Connection, Row
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Request
from pydantic import Field, StringConstraints, model_validator

from .accounts import Authenticated, require_role, require_team
from .competition import COMPETITION_ID, audit, ensure_competition, read_settings
from .contracts import EvaluationResult, Title
from .http import ApiError, Store
from .jobs import JobStore
from .sdk import Contract

RUBRIC_VERSION = "demo-rubric-v1"
SCORING_VERSION = "demo-score-v1"
router = APIRouter(prefix="/api/v1", tags=["reviews and leaderboard"])

SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL REFERENCES freeze_records(team_id),
    version INTEGER NOT NULL,
    record TEXT NOT NULL,
    UNIQUE (team_id, version)
);
CREATE TABLE IF NOT EXISTS review_assignments (
    id TEXT PRIMARY KEY,
    report_id TEXT NOT NULL REFERENCES reports(id),
    reviewer_id TEXT NOT NULL REFERENCES users(id),
    rubric_version TEXT NOT NULL,
    UNIQUE (report_id, reviewer_id)
);
CREATE TABLE IF NOT EXISTS review_versions (
    assignment_id TEXT NOT NULL REFERENCES review_assignments(id),
    revision INTEGER NOT NULL,
    record TEXT NOT NULL,
    PRIMARY KEY (assignment_id, revision)
);
CREATE TABLE IF NOT EXISTS leaderboard_exclusions (
    run_id TEXT PRIMARY KEY REFERENCES official_runs(run_id),
    status TEXT NOT NULL CHECK (status IN ('REPORT_MISSING', 'OFFICIAL_RUN_FAILED')),
    reason TEXT NOT NULL,
    decided_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS leaderboards (
    id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL UNIQUE,
    snapshot TEXT NOT NULL,
    created_at TEXT NOT NULL,
    published_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS leaderboard_published ON leaderboards((1)) WHERE published_at IS NOT NULL;
"""

ReportBody = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50_000)]
Score = Annotated[int, Field(strict=True, ge=0, le=100)]
ExclusionStatus = Literal["REPORT_MISSING", "OFFICIAL_RUN_FAILED"]


class ReportInput(Contract):
    title: Title
    body: ReportBody


class Report(ReportInput):
    id: UUID
    team_id: UUID
    version: int
    created_at: datetime


class AssignmentInput(Contract):
    report_id: UUID
    reviewer_id: UUID


class RubricScores(Contract):
    research: Score
    feedback: Score
    effectiveness: Score
    innovation: Score
    reproducibility: Score


class ReviewInput(Contract):
    revision: Annotated[int, Field(strict=True, ge=0)]
    status: Literal["DRAFT", "SUBMITTED"]
    scores: RubricScores | None = None
    comment: Annotated[str, Field(max_length=3000)] = ""

    @model_validator(mode="after")
    def require_scores(self):
        if self.status == "SUBMITTED" and self.scores is None:
            raise ValueError("submitted reviews require all rubric scores")
        return self


class Review(ReviewInput):
    updated_at: datetime


class Assignment(Contract):
    id: UUID
    reviewer_id: UUID
    report: Report
    rubric_version: str
    review: Review | None


class Standing(Contract):
    rank: int
    team_id: UUID
    team_name: str
    net_return: Decimal
    quant_score: Decimal
    report_score: Decimal
    total_score: Decimal


class ExclusionInput(Contract):
    status: ExclusionStatus
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class ExclusionDecision(ExclusionInput):
    run_id: UUID
    team_id: UUID
    decided_at: datetime


class UnrankedTeam(Contract):
    team_id: UUID
    team_name: str
    status: ExclusionStatus


class LeaderboardSnapshot(Contract):
    competition_id: str = COMPETITION_ID
    batch_id: UUID
    quant_weight_bps: int
    scoring_version: str = SCORING_VERSION
    rubric_version: str = RUBRIC_VERSION
    entries: list[Standing]
    unranked: list[UnrankedTeam] = Field(default_factory=list)


class Leaderboard(LeaderboardSnapshot):
    id: UUID
    created_at: datetime
    published_at: datetime | None


def initialize(jobs: JobStore) -> None:
    with jobs.connect() as db:
        db.executescript(SCHEMA)


def require_unpublished(db: Connection) -> None:
    if db.execute("SELECT 1 FROM leaderboards WHERE published_at IS NOT NULL").fetchone():
        raise ApiError(409, "LEADERBOARD_LOCKED", "榜单已发布，评审数据已锁定")


@router.post("/reports", status_code=201, operation_id="submitReport")
def submit_report(data: ReportInput, current: Authenticated, request: Request, jobs: Store) -> Report:
    team_id = require_team(current)
    with jobs.transaction() as db:
        require_unpublished(db)
        if not db.execute("SELECT 1 FROM freeze_records WHERE team_id = ?", (team_id,)).fetchone():
            raise ApiError(409, "REPORT_NOT_OPEN", "本队取得冻结回执后可提交报告")
        version = db.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM reports WHERE team_id = ?", (team_id,),
        ).fetchone()[0]
        now = request.app.state.clock()
        report = Report(**data.model_dump(), id=uuid4(), team_id=team_id, version=version, created_at=now)
        db.execute("INSERT INTO reports VALUES (?, ?, ?, ?)", (str(report.id), team_id, version, report.model_dump_json()))
        audit(db, current.identity.id, "submit_report", str(report.id), now, str(version))
        return report


@router.get("/reports", operation_id="listTeamReports")
def team_reports(current: Authenticated, jobs: Store) -> list[Report]:
    team_id = require_team(current)
    with jobs.connect() as db:
        rows = db.execute("SELECT record FROM reports WHERE team_id = ? ORDER BY version DESC", (team_id,)).fetchall()
    return [Report.model_validate_json(row["record"]) for row in rows]


@router.get("/admin/reports", operation_id="listCompetitionReports")
def competition_reports(current: Authenticated, jobs: Store) -> list[Report]:
    require_role(current, "organizer")
    with jobs.connect() as db:
        rows = db.execute("SELECT record FROM reports ORDER BY team_id, version DESC").fetchall()
    return [Report.model_validate_json(row["record"]) for row in rows]


ASSIGNMENT_QUERY = """
SELECT a.*, p.record AS report, v.record AS review
FROM review_assignments a
JOIN reports p ON p.id = a.report_id
LEFT JOIN review_versions v ON v.assignment_id = a.id AND v.revision = (
    SELECT MAX(revision) FROM review_versions WHERE assignment_id = a.id
)
"""


def read_assignment(row) -> Assignment:
    return Assignment(
        id=row["id"], reviewer_id=row["reviewer_id"], rubric_version=row["rubric_version"],
        report=Report.model_validate_json(row["report"]),
        review=Review.model_validate_json(row["review"]) if row["review"] else None,
    )


@router.post("/admin/review-assignments", status_code=201, operation_id="assignReview")
def assign_review(data: AssignmentInput, current: Authenticated, request: Request, jobs: Store) -> Assignment:
    organizer = require_role(current, "organizer")
    report_id, reviewer_id = str(data.report_id), str(data.reviewer_id)
    with jobs.transaction() as db:
        require_unpublished(db)
        if db.execute("SELECT 1 FROM reports WHERE id = ?", (report_id,)).fetchone() is None:
            raise ApiError(404, "REPORT_NOT_FOUND", "该报告不存在")
        if db.execute("SELECT 1 FROM users WHERE id = ? AND role = 'reviewer'", (reviewer_id,)).fetchone() is None:
            raise ApiError(404, "REVIEWER_NOT_FOUND", "该评委不存在")
        assignment_id = str(uuid4())
        added = db.execute(
            "INSERT INTO review_assignments VALUES (?, ?, ?, ?) ON CONFLICT(report_id, reviewer_id) DO NOTHING",
            (assignment_id, report_id, reviewer_id, RUBRIC_VERSION),
        ).rowcount
        if added:
            audit(db, organizer.id, "assign_review", assignment_id, request.app.state.clock(), report_id)
        row = db.execute(
            ASSIGNMENT_QUERY + " WHERE a.report_id = ? AND a.reviewer_id = ?", (report_id, reviewer_id),
        ).fetchone()
        return read_assignment(row)


@router.get("/reviews/assignments", operation_id="listReviewAssignments")
def review_assignments(current: Authenticated, jobs: Store) -> list[Assignment]:
    reviewer = require_role(current, "reviewer")
    with jobs.connect() as db:
        rows = db.execute(ASSIGNMENT_QUERY + " WHERE a.reviewer_id = ? ORDER BY a.rowid", (str(reviewer.id),)).fetchall()
    return [read_assignment(row) for row in rows]


@router.put("/reviews/{assignment_id}", operation_id="saveReview")
def save_review(assignment_id: UUID, data: ReviewInput, current: Authenticated,
                request: Request, jobs: Store) -> Review:
    reviewer = require_role(current, "reviewer")
    with jobs.transaction() as db:
        row = db.execute(
            ASSIGNMENT_QUERY + " WHERE a.id = ? AND a.reviewer_id = ?", (str(assignment_id), str(reviewer.id)),
        ).fetchone()
        if row is None:
            raise ApiError(404, "REVIEW_NOT_FOUND", "该评审任务不存在")
        require_unpublished(db)
        previous = Review.model_validate_json(row["review"]) if row["review"] else None
        if previous and previous.status == "SUBMITTED":
            raise ApiError(409, "REVIEW_SUBMITTED", "该评分已提交")
        revision = previous.revision if previous else 0
        if revision != data.revision:
            raise ApiError(409, "REVIEW_CONFLICT", "评分已更新，请重新读取版本")
        now = request.app.state.clock()
        review = Review(**data.model_dump(exclude={"revision"}), revision=revision + 1, updated_at=now)
        db.execute(
            "INSERT INTO review_versions VALUES (?, ?, ?)", (str(assignment_id), review.revision, review.model_dump_json()),
        )
        audit(db, reviewer.id, "save_review", str(assignment_id), now, f"{review.status} {review.revision}")
        return review


def points(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def latest_official_run(db: Connection, batch_id: str, team_id: str) -> Row | None:
    return db.execute(
        """SELECT r.id, r.status, r.result FROM official_runs o JOIN runs r ON r.id = o.run_id
           WHERE o.batch_id = ? AND o.team_id = ? ORDER BY o.attempt DESC LIMIT 1""",
        (batch_id, team_id),
    ).fetchone()


def latest_report(db: Connection, team_id: str) -> Row | None:
    return db.execute("SELECT id FROM reports WHERE team_id = ? ORDER BY version DESC LIMIT 1", (team_id,)).fetchone()


def confirmed_exclusion(db: Connection, run_id: str, status: ExclusionStatus) -> bool:
    return db.execute(
        "SELECT 1 FROM leaderboard_exclusions WHERE run_id = ? AND status = ?", (run_id, status),
    ).fetchone() is not None


@router.put("/admin/runs/{run_id}/leaderboard-exclusion", operation_id="recordLeaderboardExclusion")
def record_exclusion(
    run_id: UUID, data: ExclusionInput, current: Authenticated, request: Request, jobs: Store,
) -> ExclusionDecision:
    organizer = require_role(current, "organizer")
    with jobs.transaction() as db:
        require_unpublished(db)
        official = db.execute(
            "SELECT batch_id, team_id FROM official_runs WHERE run_id = ?", (str(run_id),),
        ).fetchone()
        if official is None:
            raise ApiError(404, "RUN_NOT_FOUND", "该正式任务不存在")
        team_id = official["team_id"]
        run = latest_official_run(db, official["batch_id"], team_id)
        if run is None or run["id"] != str(run_id):
            raise ApiError(409, "OFFICIAL_RUN_SUPERSEDED", "请使用本队最新正式任务登记不排名状态")
        if run["status"] == "FAILED":
            eligible_status = "OFFICIAL_RUN_FAILED"
        elif run["status"] == "SUCCEEDED" and latest_report(db, team_id) is None:
            eligible_status = "REPORT_MISSING"
        else:
            eligible_status = None
        if eligible_status != data.status:
            raise ApiError(409, "EXCLUSION_NOT_ALLOWED", "本队当前评测及报告状态不符合该不排名原因")
        previous = db.execute(
            "SELECT * FROM leaderboard_exclusions WHERE run_id = ?", (str(run_id),),
        ).fetchone()
        if previous:
            if previous["status"] != data.status or previous["reason"] != data.reason:
                raise ApiError(409, "EXCLUSION_ALREADY_RECORDED", "该正式任务已有不排名记录")
            return ExclusionDecision(team_id=team_id, **dict(previous))
        now = request.app.state.clock()
        db.execute(
            "INSERT INTO leaderboard_exclusions (run_id, status, reason, decided_at) VALUES (?, ?, ?, ?)",
            (str(run_id), data.status, data.reason, now.isoformat()),
        )
        audit(db, organizer.id, "record_leaderboard_exclusion", str(run_id), now, data.model_dump_json())
        return ExclusionDecision(**data.model_dump(), run_id=run_id, team_id=team_id, decided_at=now)


def score_entry(db: Connection, team: Row, run: Row, report: Row, weight: int) -> tuple[Standing, list[str]]:
    rows = db.execute(ASSIGNMENT_QUERY + " WHERE a.report_id = ? ORDER BY a.id", (report["id"],)).fetchall()
    if not rows:
        raise ApiError(409, "REVIEWS_INCOMPLETE", "每份最新报告需要分配评委")
    scores = []
    sources = [run["id"], report["id"]]
    for row in rows:
        review = Review.model_validate_json(row["review"]) if row["review"] else None
        if review is None or review.status != "SUBMITTED" or review.scores is None:
            raise ApiError(409, "REVIEWS_INCOMPLETE", "所有已分配评委需要提交评分")
        scores.extend(review.scores.model_dump().values())
        sources.append(f'{row["id"]}:{review.revision}')
    net_return = EvaluationResult.model_validate_json(run["result"]).metrics.net_return
    quant_score = points(max(Decimal(0), min(Decimal(100), 50 + 100 * net_return)))
    report_score = points(Decimal(sum(scores)) / len(scores))
    total_score = points((quant_score * weight + report_score * (10_000 - weight)) / 10_000)
    return Standing(
        rank=0, team_id=team["id"], team_name=team["name"], net_return=net_return,
        quant_score=quant_score, report_score=report_score, total_score=total_score,
    ), sources


def snapshot(db: Connection) -> tuple[LeaderboardSnapshot, str]:
    settings, _ = read_settings(db)
    batch = db.execute("SELECT id FROM official_batches WHERE competition_id = ?", (COMPETITION_ID,)).fetchone()
    if settings is None or batch is None:
        raise ApiError(409, "OFFICIAL_RUNS_INCOMPLETE", "正式评测尚未完成")
    teams = db.execute("SELECT t.id, t.name FROM freeze_records f JOIN teams t ON t.id = f.team_id ORDER BY t.id")
    entries = []
    unranked = []
    sources = []
    for team in teams:
        run = latest_official_run(db, batch["id"], team["id"])
        if run is None or run["status"] in {"QUEUED", "RUNNING"}:
            raise ApiError(409, "OFFICIAL_RUNS_INCOMPLETE", "全部冻结队伍需要完成正式评测")
        if run["status"] == "FAILED":
            if not confirmed_exclusion(db, run["id"], "OFFICIAL_RUN_FAILED"):
                raise ApiError(409, "OFFICIAL_RUNS_INCOMPLETE", "失败的正式评测需重跑或由组织者登记无有效成绩")
            status = "OFFICIAL_RUN_FAILED"
        else:
            report = latest_report(db, team["id"])
            if report is not None:
                entry, entry_sources = score_entry(db, team, run, report, settings.quant_weight_bps)
                entries.append(entry)
                sources.extend(entry_sources)
                continue
            if not confirmed_exclusion(db, run["id"], "REPORT_MISSING"):
                raise ApiError(409, "REPORTS_INCOMPLETE", "缺交报告需由组织者登记")
            status = "REPORT_MISSING"
        unranked.append(UnrankedTeam(team_id=team["id"], team_name=team["name"], status=status))
        sources.append(run["id"])
    entries.sort(key=lambda entry: (-entry.total_score, -entry.net_return, -entry.report_score, entry.team_id))
    rank = 0
    previous = None
    for index, entry in enumerate(entries, start=1):
        tie = (entry.total_score, entry.net_return, entry.report_score)
        if tie != previous:
            rank = index
        entries[index - 1] = entry.model_copy(update={"rank": rank})
        previous = tie
    result = LeaderboardSnapshot(
        batch_id=batch["id"], quant_weight_bps=settings.quant_weight_bps, entries=entries, unranked=unranked,
    )
    # Preserve fingerprints for previews generated before the unranked field was added.
    canonical = result.model_dump_json(exclude={"unranked"} if not unranked else None)
    fingerprint = sha256((canonical + "|" + "|".join(sources)).encode()).hexdigest()
    return result, fingerprint


def read_leaderboard(row) -> Leaderboard:
    result = LeaderboardSnapshot.model_validate_json(row["snapshot"])
    return Leaderboard(**result.model_dump(), id=row["id"], created_at=row["created_at"], published_at=row["published_at"])


@router.post("/admin/leaderboards", status_code=201, operation_id="createLeaderboard")
def create_leaderboard(current: Authenticated, request: Request, jobs: Store) -> Leaderboard:
    organizer = require_role(current, "organizer")
    with jobs.transaction() as db:
        require_unpublished(db)
        result, fingerprint = snapshot(db)
        previous = db.execute("SELECT * FROM leaderboards WHERE fingerprint = ?", (fingerprint,)).fetchone()
        if previous:
            return read_leaderboard(previous)
        leaderboard_id = str(uuid4())
        now = request.app.state.clock()
        row = db.execute(
            "INSERT INTO leaderboards VALUES (?, ?, ?, ?, NULL) RETURNING *",
            (leaderboard_id, fingerprint, result.model_dump_json(), now.isoformat()),
        ).fetchone()
        audit(db, organizer.id, "create_leaderboard", leaderboard_id, now, fingerprint)
        return read_leaderboard(row)


@router.post("/admin/leaderboards/{leaderboard_id}/publish", operation_id="publishLeaderboard")
def publish(leaderboard_id: UUID, current: Authenticated, request: Request, jobs: Store) -> Leaderboard:
    organizer = require_role(current, "organizer")
    with jobs.transaction() as db:
        row = db.execute("SELECT * FROM leaderboards WHERE id = ?", (str(leaderboard_id),)).fetchone()
        if row is None:
            raise ApiError(404, "LEADERBOARD_NOT_FOUND", "该榜单不存在")
        if row["published_at"]:
            return read_leaderboard(row)
        require_unpublished(db)
        _, fingerprint = snapshot(db)
        if fingerprint != row["fingerprint"]:
            raise ApiError(409, "LEADERBOARD_STALE", "评测或评审已变更，请重新生成榜单")
        now = request.app.state.clock()
        row = db.execute(
            "UPDATE leaderboards SET published_at = ? WHERE id = ? RETURNING *", (now.isoformat(), str(leaderboard_id)),
        ).fetchone()
        audit(db, organizer.id, "publish_leaderboard", str(leaderboard_id), now, fingerprint)
        return read_leaderboard(row)


@router.get("/competitions/{competition_id}/leaderboard", operation_id="getLeaderboard")
def published_leaderboard(competition_id: str, jobs: Store) -> Leaderboard:
    ensure_competition(competition_id)
    with jobs.connect() as db:
        row = db.execute("SELECT * FROM leaderboards WHERE published_at IS NOT NULL").fetchone()
    if row is None:
        raise ApiError(404, "LEADERBOARD_UNPUBLISHED", "榜单尚未发布")
    return read_leaderboard(row)
