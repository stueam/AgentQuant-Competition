from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from agentquant.api import create_app
from agentquant.contracts import RunError
from agentquant.worker import run_once

CONTEST = "/api/v1/competitions/track-2"
ADMIN = "/api/v1/admin"
BASELINE = {"name": "因子", "source": {"kind": "baseline", "baseline_id": "factor-rank"}}


def test_cutoff_quota_and_official_batch(tmp_path: Path, sign_in) -> None:
    now = datetime(2026, 11, 26, 12, tzinfo=UTC)
    deadline = now + timedelta(hours=1)
    ready = deadline + timedelta(hours=1)
    app = create_app(tmp_path / "jobs.sqlite3", allow_demo=False, clock=lambda: now)
    with TestClient(app) as admin, TestClient(app) as captain, TestClient(app) as member:
        sign_in(admin, "admin", "organizer")
        sign_in(captain, "captain")
        sign_in(member, "member")
        team = captain.post("/api/v1/teams", json={"name": "参赛队"}).raise_for_status().json()
        member.post("/api/v1/teams/join", json={"invite_code": team["invite_code"]}).raise_for_status()
        settings_url = f"{ADMIN}/competitions/track-2"
        settings = {
            "validation_deadline": deadline.isoformat(), "official_ready_at": ready.isoformat(),
            "daily_validation_limit": 1, "quant_weight_bps": 7000,
        }
        assert captain.put(settings_url, json=settings).status_code == 403
        assert admin.put(settings_url, json=settings).raise_for_status().json()["phase"] == "validation"
        assert admin.put(settings_url, json=settings).status_code == 409

        path = f"{CONTEST}/datasets"
        assert [row["id"] for row in captain.get(path).json()] == ["synthetic-v1"]
        assert captain.get(f"{path}/synthetic-official-v1/download").status_code == 404
        assert captain.get(f"{path}/synthetic-v1/download").json()["id"] == "synthetic-v1"
        submission = captain.post("/api/v1/submissions", json=BASELINE).raise_for_status().json()
        validation = {"dataset_id": "synthetic-v1", "submission_id": submission["id"]}
        run_url = "/api/v1/runs"
        key = {"Idempotency-Key": "first"}
        first = captain.post(run_url, json=validation, headers=key).raise_for_status().json()
        assert captain.post(run_url, json=validation, headers={"Idempotency-Key": "more"}).status_code == 429
        selection = {"submission_id": submission["id"], "revision": 0}
        select_url = f"{CONTEST}/final-selection"
        assert member.put(select_url, json=selection).status_code == 403
        assert captain.put(select_url, json=selection).raise_for_status().json()["revision"] == 1
        assert captain.put(select_url, json=selection).status_code == 409
        assert admin.post(f"{settings_url}/freeze").status_code == 409

        now = deadline
        assert captain.get(CONTEST).json()["phase"] == "awaiting_freeze"
        assert captain.put(select_url, json={**selection, "revision": 1}).status_code == 409
        assert captain.post("/api/v1/submissions", json=BASELINE).status_code == 409
        assert captain.post(run_url, json=validation, headers=key).json()["id"] == first["id"]
        freeze = admin.post(f"{settings_url}/freeze").raise_for_status().json()
        assert freeze["entries"] == 1
        assert admin.post(f"{settings_url}/freeze").json() == freeze
        assert member.get(f"{CONTEST}/freeze-receipt").json()["submission_id"] == submission["id"]
        assert admin.post(f"{ADMIN}/official-runs").status_code == 409

        now = ready
        batch = admin.post(f"{ADMIN}/official-runs").raise_for_status().json()
        old_run = batch["runs"][0]["run"]
        assert old_run["kind"] == "official"
        assert old_run["dataset_id"] == "synthetic-official-v1"
        assert len(batch["dataset_sha256"]) == 64
        assert admin.post(f"{ADMIN}/official-runs").json()["id"] == batch["id"]
        assert captain.get(f"{run_url}/{old_run['id']}").status_code == 404
        assert captain.get(f"{ADMIN}/runs/{old_run['id']}/result").status_code == 403

        jobs = app.state.jobs
        assert run_once(jobs)
        failed = jobs.claim()
        assert failed and str(failed.spec.run_id) == old_run["id"]
        assert jobs.finish(failed, RunError(code="TEST", message="模拟平台故障"))
        retry_url = f"{ADMIN}/runs/{old_run['id']}/retry"
        retry = {"reason": "恢复评测资源"}
        replacement = admin.post(retry_url, json=retry).raise_for_status().json()
        assert replacement["attempt"] == 2
        assert admin.post(retry_url, json=retry).json() == replacement
        assert run_once(jobs)
        assert not run_once(jobs)
        new_run_id = replacement["run"]["id"]
        assert admin.get(f"{ADMIN}/runs/{new_run_id}/result").json()["equity"][0]["trading_date"] == "2026-02-05"
        runs = admin.get(f"{ADMIN}/official-runs/{batch['id']}").json()["runs"]
        assert [row["run"]["status"] for row in runs] == ["FAILED", "SUCCEEDED"]
