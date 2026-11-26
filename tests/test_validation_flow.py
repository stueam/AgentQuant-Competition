from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from agentquant.api import create_app
from agentquant.demo import FactorRankStrategy, RULES, load_dataset
from agentquant.evaluator import Dataset, evaluate
from agentquant.jobs import JobStore
from agentquant.worker import run_once


def submit(client: TestClient, baseline: str = "factor-rank") -> dict:
    response = client.post("/api/v1/submissions", json={
        "name": "基线",
        "source": {"kind": "baseline", "baseline_id": baseline},
    })
    assert response.status_code == 201
    return response.json()


def test_http_worker_and_result(tmp_path: Path) -> None:
    database = tmp_path / "jobs.sqlite3"
    with TestClient(create_app(database)) as client:
        assert client.get("/api/v1/catalog").json()["datasets"][0]["id"] == "synthetic-v1"
        submission = submit(client)
        headers = {"Idempotency-Key": str(uuid4())}
        body = {"submission_id": submission["id"], "dataset_id": "synthetic-v1"}
        created = client.post("/api/v1/runs", json=body, headers=headers)
        assert created.status_code == 202
        assert created.json()["status"] == "QUEUED"
        assert client.post("/api/v1/runs", json=body, headers=headers).json()["id"] == created.json()["id"]
        other = submit(client, "cash")
        conflict = client.post(
            "/api/v1/runs", json={**body, "submission_id": other["id"]}, headers=headers,
        )
        assert conflict.status_code == 409
        run_url = f"/api/v1/runs/{created.json()['id']}"
        assert client.get(f"{run_url}/result").status_code == 409
        store = JobStore(database)
        assert run_once(store)
        assert not run_once(store)
        assert client.get(run_url).json()["status"] == "SUCCEEDED"
        result = client.get(f"{run_url}/result").json()
        assert [Decimal(point["equity"]) for point in result["equity"]] == [
            Decimal("100000"), Decimal("100489.80"), Decimal("100979.40"), Decimal("101449.32"),
        ]
        assert result["trades"][0]["asset_id"] == "A001"
        assert result["trades"][0]["shares"] == 5000
        assert client.get(f"{run_url}/metrics").json() == result["metrics"]
        assert client.get(f"{run_url}/equity").json() == result["equity"]
        page = client.get(f"{run_url}/trades?limit=1").json()
        assert page == {"offset": 0, "limit": 1, "total": len(result["trades"]), "items": result["trades"][:1]}
        assert client.get(f"{run_url}/trades?offset=10").json()["items"] == []
        assert client.get("/api/v1/submissions?limit=1&offset=1").json() == [submission]

    with TestClient(create_app(database)) as restarted:
        assert restarted.get(f"{run_url}/result").json() == result


def test_expired_worker_cannot_commit(tmp_path: Path) -> None:
    database = tmp_path / "jobs.sqlite3"
    with TestClient(create_app(database)) as client:
        submission = submit(client)
        response = client.post(
            "/api/v1/runs", headers={"Idempotency-Key": str(uuid4())},
            json={"submission_id": submission["id"], "dataset_id": "synthetic-v1"},
        )
        assert response.status_code == 202
        store = JobStore(database)
        expired = store.claim(lease_seconds=0)
        assert expired is not None
        assert run_once(store)
        result = evaluate(load_dataset("synthetic-v1"), FactorRankStrategy(), RULES)
        assert not store.finish(expired, result)


def test_future_price_changes_cannot_change_prior_trades() -> None:
    dataset = load_dataset("synthetic-v1")
    changed_rows = dataset.model_dump(mode="json")
    changed_rows["days"][3]["market"][0]["open"] = "22.00"
    changed = Dataset.model_validate(changed_rows)
    original = evaluate(dataset, FactorRankStrategy(), RULES)
    modified = evaluate(changed, FactorRankStrategy(), RULES)
    assert original.equity[:-1] == modified.equity[:-1]
    assert original.trades[:-1] == modified.trades[:-1]
    assert original.trades[-1] != modified.trades[-1]
