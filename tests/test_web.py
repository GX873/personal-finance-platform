from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from finance_app.app import create_app
from finance_app.config import Settings, get_settings
from finance_app.db import utc_now
from finance_app.portfolio.models import PortfolioSnapshot
from tests.test_auth import client as auth_client_fixture
from tests.test_auth import login
from tests.test_database import db_session as database_session_fixture

client = auth_client_fixture
db_session = database_session_fixture


@pytest.mark.parametrize("path", ["/", "/holdings", "/alerts"])
def test_dashboard_pages_require_login(client, path):
    assert client.get(path).status_code == 303
    login(client)
    response = client.get(path)
    assert response.status_code == 200
    assert 'lang="zh-CN"' in response.text
    assert 'name="viewport"' in response.text


def test_empty_dashboard_has_no_invented_balances(client):
    login(client)
    page = client.get("/").text
    assert "未确认" in page and "待确认" in page
    assert "WAIT" in page and "计划" in page
    assert "4,000.00" in page and "2,700.00" in page
    assert "尚未添加持仓" in page
    assert client.get("/static/app.css").status_code == 200


def test_partial_snapshot_does_not_claim_total(client, db_session):
    db_session.add(
        PortfolioSnapshot(
            snapshot_date=utc_now().date(),
            data_complete=False,
            known_value_cents=123456,
            total_value_cents=None,
        )
    )
    db_session.commit()
    login(client)
    page = client.get("/").text
    assert "1,234.56" in page and "已知部分" in page
    assert "未确认" in page


def test_preview_is_explicit_and_never_reads_real_data(client, monkeypatch, db_session):
    from finance_app.portfolio.models import Alert

    db_session.add(Alert(alert_type="private", severity="info", message="PRIVATE_SENTINEL"))
    db_session.commit()
    assert client.get("/preview").status_code == 404
    monkeypatch.setenv("FINANCE_DEMO_MODE", "true")
    get_settings.cache_clear()
    with TestClient(create_app()) as preview:
        page = preview.get("/preview")
        assert page.status_code == 200
        assert "演示预览" in page.text
        assert "尚未添加持仓" in page.text
        assert "PRIVATE_SENTINEL" not in page.text
        assert preview.get("/").history[0].status_code == 303


def test_production_rejects_demo_mode():
    with pytest.raises(ValueError, match="demo"):
        Settings(environment="production", secret_key="x" * 40, demo_mode=True)


def test_chart_preserves_unknown_day_gaps(db_session):
    from finance_app.web.viewmodels import dashboard

    today = utc_now().date()
    for offset in (0, 2):
        db_session.add(
            PortfolioSnapshot(
                snapshot_date=today - timedelta(days=offset),
                data_complete=True,
                total_value_cents=10000,
                known_value_cents=10000,
            )
        )
    db_session.commit()
    data = dashboard(db_session)
    assert len(data["chart_points"]) == 2
    assert data["chart_lines"] == []
