"""Team-owned ZIP uploads with bounded structural validation and immutable contents."""

import hashlib
import io
import stat
import zipfile
import zlib
from datetime import datetime
from pathlib import PurePosixPath
from sqlite3 import Connection, Row
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Request, Response
from pydantic import Field, StringConstraints
from starlette.concurrency import run_in_threadpool

from .accounts import Authenticated, require_team
from .competition import check_validation
from .contracts import ErrorResponse
from .http import ApiError, IdempotencyKey, Store
from .jobs import JobStore
from .sdk import Contract

MAX_ARCHIVE_BYTES = 2 * 1024**2
MAX_EXPANDED_BYTES = 16 * 1024**2
MAX_MEMBERS = 128
ENTRYPOINT = "strategy.py"

SCHEMA = """
CREATE TABLE IF NOT EXISTS uploads (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL REFERENCES teams(id),
    size_bytes INTEGER NOT NULL,
    expected_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    content BLOB,
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS upload_completions (
    team_id TEXT NOT NULL REFERENCES teams(id),
    idempotency_key TEXT NOT NULL,
    upload_id TEXT NOT NULL REFERENCES uploads(id),
    PRIMARY KEY (team_id, idempotency_key)
);
"""

router = APIRouter(prefix="/api/v1", tags=["uploads"])
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class UploadInput(Contract):
    size_bytes: Annotated[int, Field(strict=True, ge=1, le=MAX_ARCHIVE_BYTES)]
    sha256: Sha256


class Upload(UploadInput):
    id: UUID
    created_at: datetime
    completed_at: datetime | None
    status: Literal["PENDING", "UPLOADED", "READY"]


def initialize(jobs: JobStore) -> None:
    with jobs.connect() as db:
        db.executescript(SCHEMA)


def upload_row(db: Connection, team_id: str, upload_id: UUID) -> Row:
    row = db.execute("SELECT * FROM uploads WHERE id = ? AND team_id = ?", (str(upload_id), team_id)).fetchone()
    if row is None:
        raise ApiError(404, "UPLOAD_NOT_FOUND", "该上传不存在")
    return row


def describe(row: Row) -> Upload:
    if row["completed_at"]:
        status = "READY"
    elif row["content"] is not None:
        status = "UPLOADED"
    else:
        status = "PENDING"
    return Upload(
        id=row["id"], size_bytes=row["size_bytes"], sha256=row["expected_sha256"],
        created_at=row["created_at"], completed_at=row["completed_at"], status=status,
    )


@router.post("/uploads", status_code=201, operation_id="createUpload")
def create_upload(data: UploadInput, current: Authenticated, request: Request, jobs: Store) -> Upload:
    team_id = require_team(current)
    with jobs.transaction() as db:
        now = request.app.state.clock()
        check_validation(db, team_id, now)
        row = db.execute(
            """INSERT INTO uploads (id, team_id, size_bytes, expected_sha256, created_at)
               VALUES (?, ?, ?, ?, ?) RETURNING *""",
            (str(uuid4()), team_id, data.size_bytes, data.sha256, now.isoformat()),
        ).fetchone()
        return describe(row)


@router.get("/uploads/{upload_id}", operation_id="getUpload")
def get_upload(upload_id: UUID, current: Authenticated, jobs: Store) -> Upload:
    team_id = require_team(current)
    with jobs.connect() as db:
        return describe(upload_row(db, team_id, upload_id))


def stage_upload(jobs: JobStore, request: Request, team_id: str, upload_id: UUID, content: bytes) -> None:
    with jobs.transaction() as db:
        row = upload_row(db, team_id, upload_id)
        if row["content"] is not None:
            if row["content"] != content:
                raise ApiError(409, "UPLOAD_IMMUTABLE", "上传内容已保存，请申请新上传")
            return
        check_validation(db, team_id, request.app.state.clock())
        if len(content) != row["size_bytes"] or hashlib.sha256(content).hexdigest() != row["expected_sha256"]:
            raise ApiError(400, "UPLOAD_MISMATCH", "上传的大小或校验值与申请不符")
        db.execute("UPDATE uploads SET content = ? WHERE id = ?", (content, str(upload_id)))


@router.put(
    "/uploads/{upload_id}/content", status_code=204, response_class=Response, operation_id="putUploadContent",
    openapi_extra={"requestBody": {"required": True, "content": {
        "application/zip": {"schema": {"type": "string", "format": "binary"}},
    }}},
    responses={413: {"model": ErrorResponse}, 415: {"model": ErrorResponse}, 400: {"model": ErrorResponse}},
)
async def put_content(upload_id: UUID, request: Request, current: Authenticated, jobs: Store) -> Response:
    team_id = require_team(current)
    if request.headers.get("content-type", "").partition(";")[0].strip().lower() != "application/zip":
        raise ApiError(415, "UPLOAD_MEDIA_TYPE", "请使用 application/zip 上传策略包")
    content = bytearray()
    async for chunk in request.stream():
        if len(content) + len(chunk) > MAX_ARCHIVE_BYTES:
            raise ApiError(413, "UPLOAD_TOO_LARGE", "策略包超过大小限制")
        content.extend(chunk)
    await run_in_threadpool(stage_upload, jobs, request, team_id, upload_id, bytes(content))
    return Response(status_code=204)


def validate_archive(content: bytes) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            members = archive.infolist()
            if len(members) > MAX_MEMBERS or sum(item.file_size for item in members) > MAX_EXPANDED_BYTES:
                raise ValueError("archive expands beyond limits")
            paths: set[PurePosixPath] = set()
            files: set[PurePosixPath] = set()
            for item in members:
                name = item.filename.removesuffix("/")
                parts = name.split("/")
                mode = stat.S_IFMT(item.external_attr >> 16)
                allowed_mode = stat.S_IFDIR if item.is_dir() else stat.S_IFREG
                if (
                    item.orig_filename != item.filename
                    or any(part in {"", ".", ".."} for part in parts)
                    or any(ord(char) < 32 or char in "\\:" for char in name)
                    or mode not in {0, allowed_mode}
                    or item.flag_bits & 1
                    or item.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                ):
                    raise ValueError("unsupported archive member")
                path = PurePosixPath(name.casefold())
                if path in paths:
                    raise ValueError("duplicate archive member")
                paths.add(path)
                if not item.is_dir():
                    files.add(path)
            if any(files.intersection(path.parents) for path in paths):
                raise ValueError("archive path conflicts with a file")
            if ENTRYPOINT not in {item.filename for item in members if not item.is_dir()} or archive.testzip():
                raise ValueError("missing entrypoint or invalid CRC")
    except (ValueError, OSError, EOFError, RuntimeError, zipfile.BadZipFile, zlib.error):
        raise ApiError(400, "ARCHIVE_INVALID", "策略 ZIP 损坏、缺少 strategy.py 或含不支持的文件") from None


@router.post("/uploads/{upload_id}/complete", operation_id="completeUpload",
             responses={400: {"model": ErrorResponse}})
def complete(upload_id: UUID, key: IdempotencyKey, current: Authenticated, request: Request, jobs: Store) -> Upload:
    team_id = require_team(current)
    with jobs.transaction() as db:
        row = upload_row(db, team_id, upload_id)
        previous = db.execute(
            "SELECT upload_id FROM upload_completions WHERE team_id = ? AND idempotency_key = ?", (team_id, key),
        ).fetchone()
        if previous and previous["upload_id"] != str(upload_id):
            raise ApiError(409, "IDEMPOTENCY_CONFLICT", "相同请求标识已用于不同的请求")
        if row["completed_at"] is None:
            now = request.app.state.clock()
            check_validation(db, team_id, now)
            if row["content"] is None:
                raise ApiError(409, "UPLOAD_CONTENT_REQUIRED", "请先上传策略包")
            validate_archive(row["content"])
            row = db.execute(
                "UPDATE uploads SET completed_at = ? WHERE id = ? RETURNING *", (now.isoformat(), str(upload_id)),
            ).fetchone()
        if previous is None:
            db.execute("INSERT INTO upload_completions VALUES (?, ?, ?)", (team_id, key, str(upload_id)))
        return describe(row)


@router.get("/uploads/{upload_id}/download", operation_id="downloadUpload", response_class=Response,
            responses={200: {"content": {"application/zip": {"schema": {"type": "string", "format": "binary"}}}}})
def download(upload_id: UUID, current: Authenticated, jobs: Store) -> Response:
    team_id = require_team(current)
    with jobs.connect() as db:
        row = upload_row(db, team_id, upload_id)
    if row["completed_at"] is None:
        raise ApiError(409, "UPLOAD_INCOMPLETE", "上传尚未完成校验")
    return Response(
        row["content"], media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{upload_id}.zip"', "Cache-Control": "private, no-store"},
    )
