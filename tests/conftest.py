import pytest
from fastapi.testclient import TestClient

from agentquant.accounts import issue_invite

PASSWORD = "test-password-for-account"


def register_and_login(client: TestClient, username: str, role="participant") -> dict:
    code = issue_invite(client.app.state.jobs, role)
    credentials = {"username": username, "password": PASSWORD}
    registered = client.post("/api/v1/auth/register", json={**credentials, "invite_code": code})
    assert registered.status_code == 201
    login = client.post("/api/v1/auth/login", json=credentials)
    assert login.status_code == 200
    assert "HttpOnly" in login.headers["Set-Cookie"]
    client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
    return login.json()


@pytest.fixture
def sign_in():
    return register_and_login
