from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient

from agentquant import demo
from agentquant.api import create_app
from agentquant.contracts import RunError
from agentquant.worker import run_once

CONTEST = "/api/v1/competitions/track-2"
ADMIN = "/api/v1/admin"
REPORTS = "/api/v1/reports"
BOARD = f"{CONTEST}/leaderboard"


def test_review_access_and_versioned_publication(tmp_path, sign_in) -> None:
    now = datetime(2026, 11, 26, 12, tzinfo=UTC)
    deadline = now + timedelta(minutes=1)
    app = create_app(tmp_path / "jobs.sqlite3", allow_demo=False, clock=lambda: now)
    with (
        TestClient(app) as admin, TestClient(app) as team, TestClient(app) as outsider,
        TestClient(app) as reviewer, TestClient(app) as other_reviewer,
    ):
        sign_in(admin, "admin", "organizer")
        sign_in(team, "participant")
        sign_in(outsider, "outsider")
        reviewer_id = sign_in(reviewer, "reviewer", "reviewer")["id"]
        sign_in(other_reviewer, "reviewer2", "reviewer")
        team.post("/api/v1/teams", json={"name": "现金队"}).raise_for_status()
        outsider.post("/api/v1/teams", json={"name": "其他队"}).raise_for_status()
        assert outsider.get(BOARD).status_code == 404
        assert team.post(REPORTS, json={"title": "报告", "body": "测试"}).status_code == 409
        admin.put(f"{ADMIN}/competitions/track-2", json={
            "validation_deadline": deadline.isoformat(), "official_ready_at": deadline.isoformat(),
            "daily_validation_limit": 10, "quant_weight_bps": 6000,
        }).raise_for_status()
        submission = team.post("/api/v1/submissions", json={
            "name": "空仓", "source": {"kind": "baseline", "baseline_id": "cash"},
        }).raise_for_status().json()
        team.put(f"{CONTEST}/final-selection", json={"submission_id": submission["id"], "revision": 0}).raise_for_status()

        now = deadline
        admin.post(f"{ADMIN}/competitions/track-2/freeze").raise_for_status()
        admin.post(f"{ADMIN}/official-runs").raise_for_status()
        assert admin.post(f"{ADMIN}/leaderboards").json()["code"] == "OFFICIAL_RUNS_INCOMPLETE"
        assert run_once(app.state.jobs)
        assert admin.post(f"{ADMIN}/leaderboards").json()["code"] == "REPORTS_INCOMPLETE"
        first = team.post(REPORTS, json={"title": "报告", "body": "第一版"}).raise_for_status().json()
        assert outsider.get(REPORTS).json() == []
        assert admin.post(f"{ADMIN}/leaderboards").json()["code"] == "REVIEWS_INCOMPLETE"

        def assign(report):
            return admin.post(f"{ADMIN}/review-assignments", json={
                "report_id": report["id"], "reviewer_id": reviewer_id,
            }).raise_for_status().json()

        assignment = assign(first)
        assert assign(first)["id"] == assignment["id"]
        assert other_reviewer.get("/api/v1/reviews/assignments").json() == []
        assert reviewer.get("/api/v1/reviews/assignments").json()[0]["report"]["id"] == first["id"]
        review_url = f"/api/v1/reviews/{assignment['id']}"
        draft = {"revision": 0, "status": "DRAFT", "comment": "待补充"}
        assert other_reviewer.put(review_url, json=draft).status_code == 404
        assert reviewer.put(review_url, json=draft).raise_for_status().json()["revision"] == 1
        assert reviewer.put(review_url, json=draft).status_code == 409
        assert admin.post(f"{ADMIN}/leaderboards").json()["code"] == "REVIEWS_INCOMPLETE"
        scores = {"research": 100, "feedback": 80, "effectiveness": 90, "innovation": 70, "reproducibility": 60}
        final = {"revision": 1, "status": "SUBMITTED", "scores": scores}
        assert reviewer.put(review_url, json=final).raise_for_status().json()["revision"] == 2
        assert reviewer.put(review_url, json=final).status_code == 409
        preview = admin.post(f"{ADMIN}/leaderboards").raise_for_status().json()
        assert admin.post(f"{ADMIN}/leaderboards").json()["id"] == preview["id"]
        assert preview["published_at"] is None
        assert outsider.get(BOARD).status_code == 404

        second = team.post(REPORTS, json={"title": "报告", "body": "第二版"}).raise_for_status().json()
        assert second["version"] == 2
        assignment = assign(second)
        reviewer.put(
            f"/api/v1/reviews/{assignment['id']}", json={**final, "revision": 0},
        ).raise_for_status()
        assert admin.post(f"{ADMIN}/leaderboards/{preview['id']}/publish").json()["code"] == "LEADERBOARD_STALE"
        preview = admin.post(f"{ADMIN}/leaderboards").raise_for_status().json()
        publish_url = f"{ADMIN}/leaderboards/{preview['id']}/publish"
        assert outsider.post(publish_url).status_code == 403
        board = admin.post(publish_url).raise_for_status().json()
        assert board["published_at"]
        assert admin.post(publish_url).json() == board
        assert outsider.get(BOARD).json() == board
        assert outsider.get(CONTEST).json()["phase"] == "published"
        assert team.post(REPORTS, json={"title": "新报告", "body": "已锁定"}).status_code == 409
        assert len(board["entries"]) == 1
        result = board["entries"][0]
        assert result["rank"] == 1
        assert Decimal(result["quant_score"]) == Decimal("50")
        assert Decimal(result["report_score"]) == Decimal("80")
        assert Decimal(result["total_score"]) == Decimal("62")


def test_publish_with_audited_missing_report_and_failed_run(tmp_path, sign_in) -> None:
    now = datetime(2026, 11, 26, 12, tzinfo=UTC)
    deadline = now + timedelta(minutes=1)
    app = create_app(tmp_path / "jobs.sqlite3", allow_demo=False, clock=lambda: now)
    with (
        TestClient(app) as admin, TestClient(app) as reviewer,
        TestClient(app) as ranked, TestClient(app) as missing, TestClient(app) as failed,
    ):
        organizer_id = sign_in(admin, "organizer", "organizer")["id"]
        reviewer_id = sign_in(reviewer, "reviewer", "reviewer")["id"]
        settings_url = f"{ADMIN}/competitions/track-2"
        admin.put(settings_url, json={
            "validation_deadline": deadline.isoformat(), "official_ready_at": deadline.isoformat(),
            "daily_validation_limit": 10, "quant_weight_bps": 6000,
        }).raise_for_status()

        def enter(client, name):
            sign_in(client, name)
            team = client.post("/api/v1/teams", json={"name": name}).raise_for_status().json()
            submission = client.post("/api/v1/submissions", json={
                "name": "空仓", "source": {"kind": "baseline", "baseline_id": "cash"},
            }).raise_for_status().json()
            client.put(f"{CONTEST}/final-selection", json={
                "submission_id": submission["id"], "revision": 0,
            }).raise_for_status()
            return team["id"]

        ranked_id = enter(ranked, "ranked")
        missing_id = enter(missing, "missing")
        failed_id = enter(failed, "failed")
        now = deadline
        admin.post(f"{settings_url}/freeze").raise_for_status()
        batch = admin.post(f"{ADMIN}/official-runs").raise_for_status().json()
        runs = {row["team_id"]: row["run"]["id"] for row in batch["runs"]}
        failed_url = f"{ADMIN}/runs/{runs[failed_id]}"
        exclude_missing = f"{ADMIN}/runs/{runs[missing_id]}/leaderboard-exclusion"
        missing_decision = {"status": "REPORT_MISSING", "reason": "报告截止后核查无提交"}
        failed_decision = {"status": "OFFICIAL_RUN_FAILED", "reason": "复核后确认无有效结果"}
        assert admin.put(exclude_missing, json=missing_decision).json()["code"] == "EXCLUSION_NOT_ALLOWED"

        jobs = app.state.jobs
        for _ in runs:
            claim = jobs.claim()
            assert claim is not None
            if str(claim.spec.run_id) == runs[failed_id]:
                assert jobs.finish(claim, RunError(code="EVALUATION_FAILED", message="正式评测失败"))
            else:
                assert jobs.finish(claim, demo.run(claim.spec))
        assert missing.put(exclude_missing, json=missing_decision).status_code == 403
        recorded = admin.put(exclude_missing, json=missing_decision).raise_for_status().json()
        assert recorded["team_id"] == missing_id
        assert admin.put(exclude_missing, json=missing_decision).json() == recorded
        assert admin.put(exclude_missing, json={**missing_decision, "reason": "另一个理由"}).status_code == 409
        with jobs.connect() as db:
            audit_rows = db.execute(
                "SELECT actor_id, detail FROM audit_events WHERE action = ? AND resource_id = ?",
                ("record_leaderboard_exclusion", runs[missing_id]),
            ).fetchall()
        assert len(audit_rows) == 1
        assert audit_rows[0]["actor_id"] == organizer_id
        assert missing_decision["reason"] in audit_rows[0]["detail"]

        report = ranked.post(REPORTS, json={"title": "报告", "body": "研究结果"}).raise_for_status().json()
        assert admin.put(f"{ADMIN}/runs/{runs[ranked_id]}/leaderboard-exclusion", json=missing_decision).status_code == 409
        assignment = admin.post(f"{ADMIN}/review-assignments", json={
            "report_id": report["id"], "reviewer_id": reviewer_id,
        }).raise_for_status().json()
        reviewer.put(f"/api/v1/reviews/{assignment['id']}", json={
            "revision": 0, "status": "SUBMITTED", "scores": dict.fromkeys(
                ("research", "feedback", "effectiveness", "innovation", "reproducibility"), 80,
            ),
        }).raise_for_status()
        boards = f"{ADMIN}/leaderboards"
        assert admin.post(boards).json()["code"] == "OFFICIAL_RUNS_INCOMPLETE"
        admin.put(f"{failed_url}/leaderboard-exclusion", json=failed_decision).raise_for_status()
        preview = admin.post(boards).raise_for_status().json()

        retry = admin.post(f"{failed_url}/retry", json={"reason": "发布前再试一次"}).raise_for_status().json()
        retry_url = f"{ADMIN}/runs/{retry['run']['id']}"
        assert admin.put(f"{failed_url}/leaderboard-exclusion", json=failed_decision).json()["code"] == "OFFICIAL_RUN_SUPERSEDED"
        assert admin.post(f"{boards}/{preview['id']}/publish").json()["code"] == "OFFICIAL_RUNS_INCOMPLETE"
        claim = jobs.claim()
        assert claim is not None and str(claim.spec.run_id) == retry["run"]["id"]
        assert jobs.finish(claim, RunError(code="EVALUATION_FAILED", message="重跑失败"))
        assert admin.post(boards).json()["code"] == "OFFICIAL_RUNS_INCOMPLETE"
        admin.put(f"{retry_url}/leaderboard-exclusion", json=failed_decision).raise_for_status()
        assert admin.post(f"{boards}/{preview['id']}/publish").json()["code"] == "LEADERBOARD_STALE"

        preview = admin.post(boards).raise_for_status().json()
        board = admin.post(f"{boards}/{preview['id']}/publish").raise_for_status().json()
        assert [(row["team_id"], row["rank"]) for row in board["entries"]] == [(ranked_id, 1)]
        assert {row["team_id"]: row["status"] for row in board["unranked"]} == {
            missing_id: "REPORT_MISSING", failed_id: "OFFICIAL_RUN_FAILED",
        }
        assert all(set(row) == {"team_id", "team_name", "status"} for row in board["unranked"])
        assert missing.get(BOARD).json() == board
        assert admin.post(f"{retry_url}/retry", json={"reason": "发布后不可修改"}).json()["code"] == "LEADERBOARD_LOCKED"
