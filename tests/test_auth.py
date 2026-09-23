import re

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from finance_app.app import create_app
from finance_app.auth.models import User
from tests.test_database import db_session as database_session_fixture

db_session = database_session_fixture


@pytest.fixture
def client(db_session):
    db_session.add(
        User(username="admin", password_hash=PasswordHasher().hash("long-password-123"))
    )
    db_session.commit()
    with TestClient(create_app(), follow_redirects=False) as client:
        yield client


def token(client, path="/login"):
    return re.search(r'name="csrf_token" value="([^"]+)"', client.get(path).text)[1]


def login(client):
    return client.post(
        "/login",
        data={
            "username": "admin",
            "password": "long-password-123",
            "csrf_token": token(client),
        },
    )


def test_anonymous_page_redirect(client):
    response = client.get("/")
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


@pytest.mark.parametrize("csrf", [None, "invalid", "\u975eascii"])
def test_login_requires_csrf(client, csrf):
    token(client)
    data = {"username": "admin", "password": "long-password-123"}
    if csrf is not None:
        data["csrf_token"] = csrf
    assert client.post("/login", data=data).status_code == 403


def test_login_rotates_csrf_and_logout_clears_session(client):
    before = token(client)
    response = client.post(
        "/login",
        data={
            "username": "admin",
            "password": "long-password-123",
            "csrf_token": before,
        },
    )
    assert response.status_code == 303
    assert "Max-Age=28800" in response.headers["set-cookie"]
    assert "samesite=lax" in response.headers["set-cookie"]
    assert "httponly" in response.headers["set-cookie"]
    assert client.get("/").status_code == 200
    after = token(client, "/")
    assert after != before
    assert client.post("/logout", data={"csrf_token": before}).status_code == 403
    assert client.post("/logout").status_code == 403
    assert client.post("/logout", data={"csrf_token": after}).status_code == 303
    assert client.get("/").status_code == 303


@pytest.mark.parametrize(
    "username,password", [("admin", "wrong"), ("unknown", "long-password-123")]
)
def test_invalid_credentials_rejected(client, username, password):
    response = client.post(
        "/login",
        data={"username": username, "password": password, "csrf_token": token(client)},
    )
    assert response.status_code == 401
    assert client.get("/").status_code == 303


def test_disabled_user_loses_access(client, db_session):
    assert login(client).status_code == 303
    user = db_session.query(User).one()
    user.is_active = False
    db_session.commit()
    assert client.get("/").status_code == 303
    assert login(client).status_code == 401


def test_production_rejects_default_secret(monkeypatch):
    from finance_app.config import Settings

    monkeypatch.setenv("FINANCE_ENVIRONMENT", "production")
    with pytest.raises(ValueError, match="secret"):
        Settings(_env_file=None)


def test_init_admin_and_change_password(db_session, monkeypatch, capsys):
    from finance_app.cli import main

    monkeypatch.setattr("finance_app.cli.getpass", lambda _: "long-password-123")
    assert main(["init-admin", "--username", "admin"]) == 0
    user = db_session.query(User).one()
    assert PasswordHasher().verify(user.password_hash, "long-password-123")
    assert main(["init-admin", "--username", "other"]) == 1
    monkeypatch.setattr("finance_app.cli.getpass", lambda _: "replacement-password")
    assert main(["change-password", "--username", "admin"]) == 0
    db_session.refresh(user)
    assert PasswordHasher().verify(user.password_hash, "replacement-password")
    assert "long-password-123" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "passwords", [("short", "short"), ("long-password-123", "different-password")]
)
def test_cli_rejects_bad_passwords(db_session, monkeypatch, passwords):
    from finance_app.cli import main

    answers = iter(passwords)
    monkeypatch.setattr("finance_app.cli.getpass", lambda _: next(answers))
    assert main(["init-admin", "--username", "admin"]) == 1
    assert db_session.query(User).count() == 0


def test_api_guard_requires_active_user(client, db_session):
    from typing import Annotated

    from fastapi import Depends

    from finance_app.auth.service import require_user

    @client.app.get("/test-private")
    def private(user: Annotated[User, Depends(require_user)]):
        return {"username": user.username}

    assert client.get("/test-private").status_code == 401
    assert login(client).status_code == 303
    assert client.get("/test-private").json() == {"username": "admin"}
    user = db_session.query(User).one()
    user.is_active = False
    db_session.commit()
    assert client.get("/test-private").status_code == 401


def test_https_only_cookie(monkeypatch):
    from finance_app.config import get_settings

    monkeypatch.setenv("FINANCE_SESSION_HTTPS_ONLY", "true")
    get_settings.cache_clear()
    try:
        with TestClient(create_app(), base_url="https://testserver") as client:
            assert "secure" in client.get("/login").headers["set-cookie"].lower()
    finally:
        get_settings.cache_clear()


def test_cli_uninitialized_database_is_actionable(tmp_path, monkeypatch, capsys):
    from finance_app.cli import main
    from finance_app.config import get_settings
    from finance_app.db import reset_database_state

    monkeypatch.setenv("FINANCE_DATABASE_URL", f"sqlite:///{tmp_path / 'empty.db'}")
    get_settings.cache_clear()
    reset_database_state()
    try:
        assert main(["init-admin", "--username", "admin"]) == 1
        assert "alembic upgrade head" in capsys.readouterr().err
    finally:
        reset_database_state()
        get_settings.cache_clear()


def test_password_change_revokes_existing_session(client, db_session, monkeypatch):
    from finance_app.cli import main

    assert login(client).status_code == 303
    monkeypatch.setattr("finance_app.cli.getpass", lambda _: "replacement-password")
    assert main(["change-password", "--username", "admin"]) == 0
    assert client.get("/").status_code == 303


def test_password_verification_runs_outside_event_loop(client, monkeypatch):
    import asyncio

    from finance_app.auth import routes

    authenticate = routes.authenticate
    observed_event_loops = []

    def verify_in_worker(db, username, password):
        try:
            observed_event_loops.append(asyncio.get_running_loop())
        except RuntimeError:
            observed_event_loops.append(None)
        return authenticate(db, username, password)

    monkeypatch.setattr(routes, "authenticate", verify_in_worker)
    assert login(client).status_code == 303
    assert observed_event_loops == [None]
