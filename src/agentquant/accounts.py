"""Invited accounts, server-side sessions and team membership."""

import argparse
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from time import time
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.security import APIKeyCookie
from pydantic import Field, StringConstraints

from .contracts import Title
from .http import ApiError, Store
from .jobs import JobStore, local_database
from .sdk import Contract

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password TEXT NOT NULL,
    role TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS invites (
    digest TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    digest TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    csrf_token TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS teams (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    join_digest TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS team_members (
    user_id TEXT PRIMARY KEY REFERENCES users(id),
    team_id TEXT NOT NULL REFERENCES teams(id),
    captain INTEGER NOT NULL
);
"""

Role = Literal["participant", "organizer", "reviewer"]
Username = Annotated[str, StringConstraints(to_lower=True, pattern=r"^[a-z0-9][a-z0-9_.-]{2,39}$")]
Password = Annotated[str, Field(min_length=12, max_length=128)]
Token = Annotated[str, Field(min_length=32, max_length=128)]
SESSION_SECONDS = 12 * 60 * 60
COOKIE = "agentquant_session"
cookie = APIKeyCookie(name=COOKIE, auto_error=False)
router = APIRouter(prefix="/api/v1", tags=["accounts"])


class Login(Contract):
    username: Username
    password: Password


class Register(Login):
    invite_code: Token


class TeamCreate(Contract):
    name: Title


class JoinTeam(Contract):
    invite_code: Token


class Member(Contract):
    id: UUID
    username: str
    captain: bool


class Team(Contract):
    id: UUID
    name: str
    members: list[Member]


class CreatedTeam(Team):
    invite_code: str


class Identity(Contract):
    id: UUID
    username: str
    role: Role
    team_id: UUID | None
    captain: bool
    csrf_token: str


@dataclass(frozen=True)
class Session:
    identity: Identity
    digest: str


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def password_hash(password: str, salt: bytes) -> str:
    hashed = hashlib.scrypt(password.encode(), salt=salt, n=2**15, r=8, p=1, maxmem=64 * 1024**2)
    return f"{salt.hex()}:{hashed.hex()}"


def initialize(jobs: JobStore) -> None:
    with jobs.connect() as db:
        db.executescript(SCHEMA)


def issue_invite(jobs: JobStore, role: Role) -> str:
    token = secrets.token_urlsafe(32)
    with jobs.connect() as db:
        db.execute("INSERT INTO invites VALUES (?, ?, ?)", (digest(token), role, time() + 86400))
    return token


def session(
    request: Request, jobs: Store, token: Annotated[str | None, Depends(cookie)],
    csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> Session | None:
    if token is None:
        return None
    token_digest = digest(token)
    with jobs.connect() as db:
        row = db.execute(
            """SELECT u.id, u.username, u.role, s.csrf_token, m.team_id,
                      COALESCE(m.captain, 0) AS captain
               FROM sessions s JOIN users u ON u.id = s.user_id
               LEFT JOIN team_members m ON m.user_id = u.id
               WHERE s.digest = ? AND s.expires_at > ?""",
            (token_digest, time()),
        ).fetchone()
    if row is None:
        raise ApiError(401, "SESSION_EXPIRED", "会话已失效，请重新登录")
    if request.method not in {"GET", "HEAD", "OPTIONS"} and (
        csrf is None or not hmac.compare_digest(csrf.encode(), row["csrf_token"].encode())
    ):
        raise ApiError(403, "CSRF_INVALID", "请求校验令牌无效")
    return Session(Identity.model_validate(dict(row)), token_digest)


def require_session(current: Annotated[Session | None, Depends(session)]) -> Session:
    if current is None:
        raise ApiError(401, "LOGIN_REQUIRED", "请先登录")
    return current


Authenticated = Annotated[Session, Depends(require_session)]


def require_role(current: Session, role: Role) -> Identity:
    identity = current.identity
    if identity.role != role:
        raise ApiError(403, "ROLE_REQUIRED", "当前身份无权执行此操作")
    return identity


def require_team(current: Session) -> str:
    identity = require_role(current, "participant")
    if identity.team_id is None:
        raise ApiError(403, "TEAM_REQUIRED", "请先加入队伍")
    return str(identity.team_id)


def validation_team(request: Request, current: Annotated[Session | None, Depends(session)]) -> str:
    if current is None and request.app.state.allow_demo:
        return ""
    return require_team(require_session(current))


ValidationTeam = Annotated[str, Depends(validation_team)]


@router.post("/auth/register", status_code=201, operation_id="register")
def register(data: Register, jobs: Store) -> None:
    hashed = password_hash(data.password, secrets.token_bytes(16))
    with jobs.transaction() as db:
        invite = db.execute(
            "DELETE FROM invites WHERE digest = ? AND expires_at > ? RETURNING role",
            (digest(data.invite_code), time()),
        ).fetchone()
        if invite is None:
            raise ApiError(403, "INVITE_INVALID", "邀请码无效或已过期")
        created = db.execute(
            "INSERT INTO users VALUES (?, ?, ?, ?) ON CONFLICT(username) DO NOTHING",
            (str(uuid4()), data.username, hashed, invite["role"]),
        ).rowcount
        if not created:
            raise ApiError(409, "USERNAME_TAKEN", "用户名已被使用")


@router.post("/auth/login", operation_id="login")
def login(data: Login, request: Request, response: Response, jobs: Store) -> Identity:
    with jobs.connect() as db:
        row = db.execute(
            """SELECT u.id, u.username, u.password, u.role, m.team_id,
                      COALESCE(m.captain, 0) AS captain
               FROM users u LEFT JOIN team_members m ON m.user_id = u.id WHERE u.username = ?""",
            (data.username,),
        ).fetchone()
    salt = bytes.fromhex(row["password"].split(":")[0]) if row else bytes(16)
    hashed = password_hash(data.password, salt)
    if row is None or not hmac.compare_digest(hashed, row["password"]):
        raise ApiError(401, "LOGIN_FAILED", "用户名或密码错误")
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    with jobs.connect() as db:
        db.execute("DELETE FROM sessions WHERE expires_at <= ? OR digest = ?",
                   (time(), digest(request.cookies.get(COOKIE, ""))))
        db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?)",
                   (digest(token), row["id"], csrf, time() + SESSION_SECONDS))
    response.set_cookie(COOKIE, token, httponly=True, secure=request.app.state.secure_cookie,
                        samesite="lax", max_age=SESSION_SECONDS)
    response.headers["Cache-Control"] = "no-store"
    return Identity(id=row["id"], username=row["username"], role=row["role"], team_id=row["team_id"],
                    captain=row["captain"], csrf_token=csrf)


@router.post("/auth/logout", status_code=204, operation_id="logout")
def logout(current: Authenticated, response: Response, jobs: Store) -> None:
    with jobs.connect() as db:
        db.execute("DELETE FROM sessions WHERE digest = ?", (current.digest,))
    response.delete_cookie(COOKIE)


@router.get("/me", operation_id="getMe")
def me(current: Authenticated, response: Response) -> Identity:
    response.headers["Cache-Control"] = "no-store"
    return current.identity


def read_team(db, team_id: str) -> Team:
    team = db.execute("SELECT id, name FROM teams WHERE id = ?", (team_id,)).fetchone()
    members = db.execute(
        """SELECT u.id, u.username, m.captain FROM team_members m JOIN users u ON u.id = m.user_id
           WHERE m.team_id = ? ORDER BY m.captain DESC, u.username""",
        (team_id,),
    )
    return Team(**dict(team), members=[Member.model_validate(dict(row)) for row in members])


def add_member(db, identity: Identity, team_id: str, captain: bool) -> None:
    added = db.execute(
        "INSERT INTO team_members VALUES (?, ?, ?) ON CONFLICT(user_id) DO NOTHING",
        (str(identity.id), team_id, captain),
    ).rowcount
    if not added:
        raise ApiError(409, "ALREADY_IN_TEAM", "每位参赛者只能加入一支队伍")


@router.post("/teams", status_code=201, operation_id="createTeam")
def create_team(data: TeamCreate, current: Authenticated, jobs: Store) -> CreatedTeam:
    identity = require_role(current, "participant")
    team_id, code = str(uuid4()), secrets.token_urlsafe(32)
    with jobs.transaction() as db:
        created = db.execute(
            "INSERT INTO teams VALUES (?, ?, ?) ON CONFLICT(name) DO NOTHING", (team_id, data.name, digest(code)),
        ).rowcount
        if not created:
            raise ApiError(409, "TEAM_NAME_TAKEN", "队名已被使用")
        add_member(db, identity, team_id, True)
        return CreatedTeam(**read_team(db, team_id).model_dump(), invite_code=code)


@router.post("/teams/join", operation_id="joinTeam")
def join_team(data: JoinTeam, current: Authenticated, jobs: Store) -> Team:
    identity = require_role(current, "participant")
    with jobs.transaction() as db:
        row = db.execute("SELECT id FROM teams WHERE join_digest = ?", (digest(data.invite_code),)).fetchone()
        if row is None:
            raise ApiError(404, "TEAM_NOT_FOUND", "组队邀请码无效")
        add_member(db, identity, row["id"], False)
        return read_team(db, row["id"])


@router.get("/teams/{team_id}", operation_id="getTeam")
def get_team(team_id: UUID, current: Authenticated, jobs: Store) -> Team:
    if require_team(current) != str(team_id):
        raise ApiError(404, "TEAM_NOT_FOUND", "该队伍不存在")
    with jobs.connect() as db:
        return read_team(db, str(team_id))


def main() -> None:
    parser = argparse.ArgumentParser(description="Issue a one-time registration invite valid for 24 hours.")
    parser.add_argument("role", choices=("participant", "organizer", "reviewer"))
    args = parser.parse_args()
    jobs = JobStore(local_database())
    initialize(jobs)
    print(issue_invite(jobs, args.role))


if __name__ == "__main__":
    main()
