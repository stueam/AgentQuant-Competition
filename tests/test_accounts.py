from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from agentquant.api import create_app
from agentquant.contracts import CreateRun, CreateSubmission
from agentquant.jobs import JobStore
from agentquant.worker import run_once

def test_team_session_and_resource_isolation(tmp_path: Path, sign_in) -> None:
    app = create_app(tmp_path / "jobs.sqlite3", allow_demo=False)
    with TestClient(app) as first, TestClient(app) as second, TestClient(app) as member:
        assert first.get("/api/v1/submissions").status_code == 401
        identity = sign_in(first, "captain")
        assert first.get("/api/v1/submissions").status_code == 403
        assert first.post("/api/v1/teams", json={"name": "甲队"}, headers={"X-CSRF-Token": "wrong"}).status_code == 403
        team = first.post("/api/v1/teams", json={"name": "甲队"}).json()
        assert first.get("/api/v1/me").json()["captain"]
        sign_in(member, "teammate")
        joined = member.post("/api/v1/teams/join", json={"invite_code": team["invite_code"]})
        assert joined.status_code == 200
        assert [row["username"] for row in joined.json()["members"]] == ["captain", "teammate"]
        assert member.post("/api/v1/teams", json={"name": "第三队"}).status_code == 409
        sign_in(second, "other")
        second.post("/api/v1/teams", json={"name": "乙队"}).raise_for_status()
        assert second.get(f"/api/v1/teams/{team['id']}").status_code == 404

        body = {"name": "基线", "source": {"kind": "baseline", "baseline_id": "cash"}}
        a = first.post("/api/v1/submissions", json=body).json()
        b = second.post("/api/v1/submissions", json=body).json()
        assert member.get("/api/v1/submissions").json() == [a]
        assert second.get("/api/v1/submissions").json() == [b]
        key = {"Idempotency-Key": "same-key"}
        validation = {"dataset_id": "synthetic-v1", "submission_id": a["id"]}
        forbidden = second.post("/api/v1/runs", json=validation, headers=key)
        assert forbidden.status_code == 404
        assert forbidden.headers["X-Request-ID"] == forbidden.json()["request_id"]
        run_a = first.post("/api/v1/runs", json=validation, headers=key).json()
        assert member.post("/api/v1/runs", json=validation, headers=key).json()["id"] == run_a["id"]
        run_b = second.post("/api/v1/runs", json={**validation, "submission_id": b["id"]}, headers=key).json()
        assert run_a["id"] != run_b["id"]
        assert run_once(app.state.jobs)
        assert run_once(app.state.jobs)
        url = f"/api/v1/runs/{run_a['id']}"
        assert member.get(f"{url}/result").status_code == 200
        assert second.get(url).status_code == 404
        assert second.get(f"{url}/result").status_code == 404
        assert second.get(f"{url}/trades").status_code == 404
        assert first.get("/api/v1/me").json()["id"] == identity["id"]
        assert first.post("/api/v1/auth/logout").status_code == 204
        assert first.get("/api/v1/me").status_code == 401


def test_concurrent_replays_enqueue_once(tmp_path: Path) -> None:
    jobs = JobStore(tmp_path / "jobs.sqlite3")
    team = str(uuid4())
    submission = jobs.create_submission(
        CreateSubmission(name="现金", source={"kind": "baseline", "baseline_id": "cash"}), team,
    )
    request = CreateRun(submission_id=submission.id, dataset_id="synthetic-v1")
    with ThreadPoolExecutor(max_workers=4) as callers:
        runs = list(callers.map(lambda _: jobs.create_run(request, "retry", "demo-v1", team), range(8)))
    assert len({run.id for run in runs}) == 1
    assert run_once(jobs)
    assert not run_once(jobs)
