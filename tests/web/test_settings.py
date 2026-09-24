from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from finance_app.ledger.models import AuditEvent
from finance_app.notifications.models import AppSetting, NotificationChannel
from finance_app.portfolio.models import AllocationTarget
from tests.web.test_forms import (
    client as web_client_fixture,
)
from tests.web.test_forms import (
    csrf,
    login,
)
from tests.web.test_forms import (
    db_session as database_session_fixture,
)

client = web_client_fixture
db_session = database_session_fixture


def valid_form(client):
    return {
        "csrf_token": csrf(client, "/settings"),
        "daily_schedule": "09:00",
        "reserve_target": "1000.00",
        "core_target_percent": "70.00",
        "satellite_target_percent": "30.00",
        "satellite_upper_percent": "10.00",
        "stale_threshold_hours": "36",
        "enabled_channels": ["email", "pushplus"],
    }


@pytest.mark.parametrize("path", ["/settings", "/analysis"])
def test_new_pages_require_login(client, path):
    response = client.get(path)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_settings_never_render_or_accept_credentials(client, monkeypatch):
    monkeypatch.setenv("FINANCE_SMTP_AUTHORIZATION_CODE", "smtp-super-secret")
    monkeypatch.setenv("FINANCE_PUSHPLUS_TOKEN", "pushplus-super-secret")
    from finance_app.config import get_settings

    get_settings.cache_clear()
    assert login(client).status_code == 303

    page = client.get("/settings")

    assert page.status_code == 200
    assert "smtp-super-secret" not in page.text
    assert "pushplus-super-secret" not in page.text
    assert "已配置" in page.text
    assert 'name="smtp_authorization_code"' not in page.text
    assert 'name="pushplus_token"' not in page.text

    data = valid_form(client)
    data["smtp_authorization_code"] = "attacker-replacement"
    response = client.post("/settings", data=data)
    assert response.status_code == 422


def test_settings_save_non_secrets_with_csrf_prg_and_audit(client, db_session):
    assert login(client).status_code == 303

    response = client.post("/settings", data=valid_form(client))

    assert response.status_code == 303
    assert response.headers["location"] == "/settings"
    db_session.expire_all()
    values = {
        row.key: row.value for row in db_session.scalars(select(AppSetting)).all()
    }
    assert values == {
        "daily_schedule": "09:00",
        "reserve_target_cents": "100000",
        "stale_threshold_hours": "36",
    }
    targets = {
        row.name: (row.target_bps, row.upper_bps)
        for row in db_session.scalars(select(AllocationTarget)).all()
    }
    assert targets == {
        "core": (7000, None),
        "satellite": (3000, None),
        "satellite_each": (0, 1000),
    }
    channels = {
        row.channel_type: row.enabled
        for row in db_session.scalars(select(NotificationChannel)).all()
    }
    assert channels == {
        "email": True,
        "pushplus": True,
        "serverchan": False,
        "wecom": False,
    }
    event = db_session.scalar(
        select(AuditEvent).where(AuditEvent.event_type == "settings.updated")
    )
    assert event is not None
    details = json.loads(event.details_json)
    rendered = json.dumps(details)
    assert details["actor"]["username"] == "admin"
    assert details["entity"] == {"type": "settings", "id": event.entity_id}
    assert details["request_id"]
    assert "credential" not in rendered.lower()
    assert "secret" not in rendered.lower()


def test_settings_toggle_existing_channel_names_without_creating_duplicates(
    client, db_session
):
    db_session.add_all(
        [
            NotificationChannel(
                name="Primary email", channel_type="email", enabled=False
            ),
            NotificationChannel(
                name="WeChat backup", channel_type="pushplus", enabled=True
            ),
        ]
    )
    db_session.commit()
    assert login(client).status_code == 303
    page = client.get("/settings")
    assert 'value="Primary email"' in page.text
    assert 'value="WeChat backup"' in page.text
    data = valid_form(client)
    data["enabled_channels"] = ["Primary email"]

    response = client.post("/settings", data=data)

    assert response.status_code == 303
    db_session.expire_all()
    channels = list(db_session.scalars(select(NotificationChannel)))
    assert [(row.name, row.enabled) for row in channels] == [
        ("Primary email", True),
        ("WeChat backup", False),
    ]


@pytest.mark.parametrize(
    "field,value",
    [
        ("daily_schedule", "9:00"),
        ("reserve_target", "999999999999999999999.00"),
        ("core_target_percent", "70.001"),
        ("core_target_percent", "80.00"),
        ("satellite_upper_percent", "31.00"),
        ("stale_threshold_hours", "0"),
        ("stale_threshold_hours", "721"),
        ("enabled_channels", "https://evil.example/token"),
    ],
)
def test_settings_strict_validation_is_atomic(client, db_session, field, value):
    assert login(client).status_code == 303
    data = valid_form(client)
    data[field] = value

    response = client.post("/settings", data=data)

    assert response.status_code == 422
    db_session.expire_all()
    assert db_session.scalar(select(AppSetting)) is None
    assert db_session.scalar(select(AllocationTarget)) is None
    assert db_session.scalar(select(NotificationChannel)) is None
    assert db_session.scalar(select(AuditEvent)) is None


def test_settings_post_requires_csrf(client):
    assert login(client).status_code == 303
    assert client.post("/settings", data={}).status_code == 403


def test_analysis_shows_unknown_total_and_no_fabricated_market_data(client):
    assert login(client).status_code == 303

    page = client.get("/analysis")

    assert page.status_code == 200
    assert "总资产未知" in page.text
    assert "等待数据" in page.text
    assert "不推测行情" in page.text
