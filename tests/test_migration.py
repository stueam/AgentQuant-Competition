import json
import sqlite3
from contextlib import closing
from uuid import uuid4

from fastapi.testclient import TestClient

from agentquant.api import create_app
from agentquant.worker import run_once

LEGACY_SCHEMA = """
CREATE TABLE submissions (id TEXT PRIMARY KEY, record TEXT NOT NULL);
CREATE TABLE runs (
    id TEXT PRIMARY KEY,
    submission_id TEXT NOT NULL REFERENCES submissions(id),
    idempotency_key TEXT NOT NULL UNIQUE,
    request TEXT NOT NULL,
    spec TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    lease_until REAL,
    lease_token TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    result TEXT,
    error TEXT
);
"""


def test_existing_demo_submissions_and_queue_survive_upgrade(tmp_path) -> None:
    path = tmp_path / "legacy.sqlite3"
    submission_id, run_id = str(uuid4()), str(uuid4())
    created_at = "2026-01-01T00:00:00+00:00"
    body = {"submission_id": submission_id, "dataset_id": "synthetic-v1"}
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript(LEGACY_SCHEMA)
        db.execute("INSERT INTO submissions VALUES (?, ?)", (submission_id, json.dumps({
            "id": submission_id, "name": "原有基线", "created_at": created_at,
            "source": {"kind": "baseline", "baseline_id": "cash"},
        })))
        db.execute(
            """INSERT INTO runs (id, submission_id, idempotency_key, request, spec, status, created_at)
               VALUES (?, ?, 'legacy-key', ?, ?, 'QUEUED', ?)""",
            (run_id, submission_id, json.dumps(body), json.dumps({
                **body, "run_id": run_id, "baseline_id": "cash", "rules_version": "demo-v1",
            }), created_at),
        )

    app = create_app(path)
    with TestClient(app) as client:
        assert client.get("/api/v1/submissions").json()[0]["id"] == submission_id
        reused = client.post("/api/v1/runs", json=body, headers={"Idempotency-Key": "legacy-key"})
        assert reused.json()["id"] == run_id
        assert reused.json()["kind"] == "validation"
        assert run_once(app.state.jobs)
    with TestClient(create_app(path)) as client:
        assert client.get(f"/api/v1/runs/{run_id}/result").status_code == 200
