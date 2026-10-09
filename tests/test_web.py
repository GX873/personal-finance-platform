from datetime import UTC, timedelta

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
    assert "待补充" in page
    assert '<div class="total-number"><span class="currency">¥</span> 待补充</div>' in page
    assert "WAIT" in page
    assert "2,700.00" in page
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
    assert "待补充" in page


def test_dashboard_uses_recorded_cash_and_prices_without_stale_confirmation(
    db_session, monkeypatch
):
    from datetime import UTC, datetime
    from decimal import Decimal

    from finance_app.ledger.models import Account, Asset, CashBucket, Holding
    from finance_app.portfolio.models import PortfolioSnapshot, PriceSnapshot
    from finance_app.web.viewmodels import dashboard

    now = datetime(2026, 10, 10, 8, tzinfo=UTC)
    monkeypatch.setattr("finance_app.web.viewmodels.utc_now", lambda: now)
    account = Account(name="cash", kind="cash", currency="CNY")
    asset = Asset(code="000001", market="CN", name="fund", currency="CNY")
    db_session.add_all([account, asset])
    db_session.flush()
    db_session.add_all(
        [
            Holding(account_id=account.id, asset_id=asset.id, quantity=Decimal(10)),
            CashBucket(bucket_kind="reserve", balance_cents=100000, updated_at=now),
            CashBucket(bucket_kind="investment", balance_cents=20000, updated_at=now),
            PriceSnapshot(
                asset_id=asset.id,
                valuation_date=now.date(),
                source="eastmoney",
                price=Decimal(10),
                quote_type="official_nav",
                fetched_at=now,
            ),
            PortfolioSnapshot(
                snapshot_date=now.date(),
                data_complete=False,
                total_value_cents=None,
                known_value_cents=100000,
                invested_value_cents=100000,
                cash_value_cents=120000,
                details_json='{"holdings_fresh":false,"cash_fresh":false}',
            ),
        ]
    )
    db_session.commit()

    data = dashboard(db_session)

    assert data["total"] == "1,300.00"
    assert data["advice_action"] == "HOLD"
    assert data["cash_total"] == "1,200.00"


def test_analysis_uses_chinese_cash_labels_without_stale_confirmation_copy(
    client, db_session
):
    from finance_app.ledger.models import CashBucket

    db_session.add_all(
        [
            CashBucket(bucket_kind="reserve", balance_cents=100000),
            CashBucket(bucket_kind="investment", balance_cents=20000),
        ]
    )
    db_session.commit()
    login(client)

    page = client.get("/analysis").text

    assert "生活备用金" in page
    assert "可投资现金" in page
    assert "reserve" not in page
    assert "investment" not in page
    assert "请确认是否陈旧" not in page


def test_dashboard_exposes_cash_and_role_chart_data(db_session):
    from finance_app.ledger.models import CashBucket

    db_session.add_all(
        [
            CashBucket(bucket_kind="reserve", balance_cents=100000),
            CashBucket(bucket_kind="investment", balance_cents=20000),
        ]
    )
    db_session.commit()

    from finance_app.web.viewmodels import dashboard

    data = dashboard(db_session)

    assert data["cash_chart"]["total_cents"] == 120000
    assert data["cash_chart"]["segments"][0]["label"] == "生活备用金"
    assert data["cash_chart"]["segments"][1]["label"] == "可投资现金"
    assert data["role_chart"]["total_cents"] == 0


def test_preview_is_explicit_and_never_reads_real_data(client, monkeypatch, db_session):
    from finance_app.portfolio.models import Alert

    db_session.add(
        Alert(alert_type="private", severity="info", message="PRIVATE_SENTINEL")
    )
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


def test_complete_snapshot_is_not_labeled_partial(client, db_session):
    from finance_app.portfolio.rules import SHANGHAI

    db_session.add(
        PortfolioSnapshot(
            snapshot_date=utc_now().astimezone(SHANGHAI).date(),
            data_complete=True,
            total_value_cents=123456,
            known_value_cents=123456,
        )
    )
    db_session.commit()
    login(client)
    page = client.get("/").text
    assert "1,234.56" in page
    assert "已知部分" not in page
    assert "不等同于当前完整总资产" not in page


def test_fresh_complete_data_waits_for_configuration_then_stales(
    db_session, monkeypatch
):
    import json
    from datetime import datetime

    from finance_app.ledger.models import CashBucket
    from finance_app.portfolio.rules import SHANGHAI
    from finance_app.web.viewmodels import dashboard

    now = datetime(2026, 9, 23, 15, tzinfo=UTC)
    monkeypatch.setattr("finance_app.web.viewmodels.utc_now", lambda: now)
    db_session.add_all(
        [
            CashBucket(bucket_kind="reserve", balance_cents=100000, updated_at=now),
            CashBucket(bucket_kind="investment", balance_cents=20000, updated_at=now),
            PortfolioSnapshot(
                snapshot_date=now.astimezone(SHANGHAI).date(),
                data_complete=True,
                total_value_cents=120000,
                known_value_cents=120000,
                details_json=json.dumps(
                    {
                        "as_of": now.isoformat(),
                        "cash_fresh": True,
                        "holdings_fresh": True,
                        "currency_supported": True,
                    }
                ),
            ),
        ]
    )
    db_session.commit()
    data = dashboard(db_session)
    assert data["known"] is None
    assert data["advice_action"] == "HOLD"
    assert data["advice_status"] == "已录入"
    assert data["freshness_label"] == "数据已录入"
    monkeypatch.setattr(
        "finance_app.web.viewmodels.utc_now", lambda: now + timedelta(days=2)
    )
    stale = dashboard(db_session)
    assert stale["advice_action"] == "HOLD"
    assert stale["freshness_label"] == "数据已录入"


@pytest.mark.parametrize(
    "section, forbidden",
    [
        (
            "alerts",
            ("holdings", "price_snapshots", "portfolio_snapshots", "cash_buckets"),
        ),
        ("holdings", ("alerts", "portfolio_snapshots", "cash_buckets")),
    ],
)
def test_views_load_only_their_own_sections(db_session, section, forbidden):
    from sqlalchemy import event

    from finance_app.web.viewmodels import dashboard

    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.lower())

    event.listen(db_session.bind, "before_cursor_execute", capture)
    try:
        dashboard(db_session, section=section)
    finally:
        event.remove(db_session.bind, "before_cursor_execute", capture)
    assert all(
        not any(f"from {table}" in sql for table in forbidden) for sql in statements
    )


def test_history_and_prices_use_bounded_queries(db_session):
    from decimal import Decimal

    from sqlalchemy import event

    from finance_app.ledger.models import Account, Asset, Holding
    from finance_app.portfolio.models import PriceSnapshot
    from finance_app.portfolio.rules import SHANGHAI
    from finance_app.web.viewmodels import dashboard

    today = utc_now().astimezone(SHANGHAI).date()
    account = Account(name="test", kind="cash")
    asset = Asset(name="test", code="000001", market="CN")
    db_session.add_all([account, asset])
    db_session.flush()
    db_session.add(
        Holding(account_id=account.id, asset_id=asset.id, quantity=Decimal(1))
    )
    for offset in range(40):
        day = today - timedelta(days=offset)
        db_session.add_all(
            [
                PortfolioSnapshot(
                    snapshot_date=day,
                    data_complete=True,
                    total_value_cents=10000,
                    known_value_cents=10000,
                ),
                PriceSnapshot(
                    asset_id=asset.id,
                    valuation_date=day,
                    source="manual",
                    price=Decimal(1),
                ),
            ]
        )
    db_session.commit()
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.lower())

    event.listen(db_session.bind, "before_cursor_execute", capture)
    try:
        data = dashboard(db_session)
    finally:
        event.remove(db_session.bind, "before_cursor_execute", capture)
    assert len(data["chart_points"]) == 7
    assert data["holdings"][0]["date"] == today.isoformat()
    bounded = [
        sql
        for sql in statements
        if "from portfolio_snapshots" in sql or "from price_snapshots" in sql
    ]
    assert bounded and all("limit" in sql for sql in bounded)


def test_intraday_estimate_only_holding_is_unpriced(db_session, monkeypatch):
    from datetime import datetime
    from decimal import Decimal

    from finance_app.ledger.models import Account, Asset, Holding
    from finance_app.portfolio.models import PriceSnapshot
    from finance_app.web.viewmodels import dashboard

    now = datetime(2026, 9, 23, 8, tzinfo=UTC)
    monkeypatch.setattr("finance_app.web.viewmodels.utc_now", lambda: now)
    account = Account(name="cash", kind="cash", currency="CNY")
    asset = Asset(code="000001", market="CN", name="estimate fund")
    db_session.add_all([account, asset])
    db_session.flush()
    db_session.add(
        Holding(account_id=account.id, asset_id=asset.id, quantity=Decimal(10))
    )
    db_session.add(
        PriceSnapshot(
            asset_id=asset.id,
            valuation_date=now.date(),
            source="efinance:estimate",
            price=Decimal("1.2"),
            quote_type="intraday_estimate",
            fetched_at=now,
        )
    )
    db_session.commit()

    data = dashboard(db_session)

    assert data["holdings"][0]["nav"] == "待补充"
    assert "estimate" not in data["holdings"][0]
    assert data["holdings"][0]["priced"] is False
    assert data["priced_total"] == 0
