import io
import stat
from hashlib import sha256
from zipfile import ZipFile, ZipInfo

import pytest
from fastapi.testclient import TestClient

from agentquant.api import create_app
from agentquant.http import ApiError
from agentquant.uploads import MAX_ARCHIVE_BYTES, validate_archive


def make_zip(*names: str | ZipInfo) -> bytes:
    buffer = io.BytesIO()
    with ZipFile(buffer, "w") as archive:
        for name in names:
            archive.writestr(name, "# fixture")
    return buffer.getvalue()


@pytest.mark.parametrize("member", ["../outside.txt", "/outside.txt", "STRATEGY.PY"])
def test_reject_invalid_archive_paths(member: str) -> None:
    with pytest.raises(ApiError, match="ZIP"):
        validate_archive(make_zip("strategy.py", member))


def test_reject_symbolic_links() -> None:
    link = ZipInfo("linked.txt")
    link.create_system = 3
    link.external_attr = stat.S_IFLNK << 16
    with pytest.raises(ApiError, match="ZIP"):
        validate_archive(make_zip("strategy.py", link))


def test_upload_and_strategy_versioning(tmp_path, sign_in) -> None:
    app = create_app(tmp_path / "jobs.sqlite3", allow_demo=False)
    with TestClient(app) as owner, TestClient(app) as other:
        sign_in(owner, "owner")
        sign_in(other, "other")
        owner.post("/api/v1/teams", json={"name": "所有者"}).raise_for_status()
        other.post("/api/v1/teams", json={"name": "其他队"}).raise_for_status()
        archive = make_zip("strategy.py", "README.txt")
        checksum = sha256(archive).hexdigest()
        upload_input = {"size_bytes": len(archive), "sha256": checksum}
        first = owner.post("/api/v1/uploads", json=upload_input).raise_for_status().json()
        second = owner.post("/api/v1/uploads", json=upload_input).raise_for_status().json()
        path = f"/api/v1/uploads/{first['id']}"
        assert first["status"] == "PENDING"
        assert other.get(path).status_code == 404
        assert owner.get(f"{path}/download").status_code == 409

        submission_body = {"name": "策略包", "source": {"kind": "archive", "upload_id": first["id"]}}
        assert owner.post("/api/v1/submissions", json=submission_body).status_code == 404
        completion = {"Idempotency-Key": "check"}
        assert owner.post(f"{path}/complete", headers=completion).json()["code"] == "UPLOAD_CONTENT_REQUIRED"
        zip_headers = {"Content-Type": "application/zip"}
        assert owner.put(f"{path}/content", content=archive).status_code == 415
        assert owner.put(f"{path}/content", content=b"x" * (MAX_ARCHIVE_BYTES + 1), headers=zip_headers).status_code == 413
        assert owner.put(f"{path}/content", content=b"incorrect", headers=zip_headers).status_code == 400
        assert other.put(f"{path}/content", content=archive, headers=zip_headers).status_code == 404
        assert owner.put(f"{path}/content", content=archive, headers=zip_headers).status_code == 204
        assert owner.get(path).json()["status"] == "UPLOADED"
        assert owner.put(f"{path}/content", content=b"new contents", headers=zip_headers).status_code == 409

        ready = owner.post(f"{path}/complete", headers=completion).raise_for_status().json()
        assert ready["status"] == "READY"
        assert owner.post(f"{path}/complete", headers=completion).json() == ready
        assert owner.post(f"/api/v1/uploads/{second['id']}/complete", headers=completion).json()["code"] == "IDEMPOTENCY_CONFLICT"
        assert owner.get(f"{path}/download").content == archive
        assert other.get(f"{path}/download").status_code == 404
        assert other.post("/api/v1/submissions", json=submission_body).status_code == 404
        saved = owner.post("/api/v1/submissions", json=submission_body).raise_for_status().json()
        assert saved["version"] == 1
        assert saved["artifact_sha256"] == checksum
        next_version = owner.post("/api/v1/submissions", json={**submission_body, "name": "新版本"}).json()
        assert next_version["version"] == 2
        assert owner.post("/api/v1/runs", json={
            "dataset_id": "synthetic-v1", "submission_id": saved["id"],
        }, headers={"Idempotency-Key": "archive-run"}).json()["code"] == "STRATEGY_SMOKE_TEST_REQUIRED"
        assert owner.put("/api/v1/competitions/track-2/final-selection", json={
            "submission_id": saved["id"], "revision": 0,
        }).json()["code"] == "STRATEGY_SMOKE_TEST_REQUIRED"
