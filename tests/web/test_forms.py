from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from alembic.config import Config
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from alembic import command
from finance_app.app import create_app
from finance_app.auth.models import User
from finance_app.db import create_db_engine, get_session_factory, reset_database_state
from finance_app.ledger.models import (
    Account,
    Asset,
    AuditEvent,
    CashBucket,
    Holding,
    Transaction,
)
from finance_app.market.base import FundNavQuote, QuoteType
from finance_app.portfolio.models import Alert, PortfolioSnapshot, PriceSnapshot
from finance_app.portfolio.service import create_daily_snapshot
from finance_app.web import routes as web_routes
from finance_app.web.forms import FormError, parse_decimal, parse_yuan


@pytest.fixture
def db_session(tmp_path, monkeypatch):
    database_url = f"sqlite:///{tmp_path / 'forms.db'}"
    monkeypatch.setenv("FINANCE_DATABASE_URL", database_url)
    from finance_app.config import get_settings

    get_settings.cache_clear()
    reset_database_state()
    command.upgrade(Config("alembic.ini"), "head")
    engine = create_db_engine(database_url)
    session = get_session_factory(engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
        reset_database_state()
        get_settings.cache_clear()


@pytest.fixture
def client(db_session: Session):
    db_session.add(
        User(username="admin", password_hash=PasswordHasher().hash("long-password-123"))
    )
    db_session.commit()
    with TestClient(create_app(), follow_redirects=False) as test_client:
        yield test_client


def login(client: TestClient):
    return client.post(
        "/login",
        data={
            "username": "admin",
            "password": "long-password-123",
            "csrf_token": csrf(client, "/login"),
        },
    )


def csrf(client, path: str) -> str:
    response = client.get(path)
    assert response.status_code == 200
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match is not None
    return match[1]


def form_markup(html: str, action: str) -> str:
    match = re.search(
        rf'<form\b[^>]*action="{re.escape(action)}"[^>]*>.*?</form>',
        html,
        re.DOTALL,
    )
    assert match is not None
    return match.group(0)


def add_account(db_session, *, opening_balance_cents: int = 100_000) -> Account:
    account = Account(
        name="工资卡",
        kind="cash",
        currency="CNY",
        opening_balance_cents=opening_balance_cents,
    )
    db_session.add(account)
    db_session.commit()
    return account


def add_asset(db_session) -> Asset:
    asset = Asset(code="000001", market="CN", name="测试基金")
    db_session.add(asset)
    db_session.commit()
    return asset


def add_held_fund(db_session: Session) -> Asset:
    account = Account(
        name="fund-account",
        kind="investment",
        currency="CNY",
        opening_balance_cents=0,
    )
    asset = Asset(
        code="000001",
        market="CN",
        name="held-fund",
        asset_class="fund",
        currency="CNY",
    )
    db_session.add_all([account, asset])
    db_session.flush()
    db_session.add(
        Holding(
            account_id=account.id,
            asset_id=asset.id,
            quantity=Decimal(10),
            cost_cents=1_000,
        )
    )
    db_session.commit()
    return asset


def post_transfer_in(client, db_session, account: Account) -> Transaction:
    assert login(client).status_code == 303
    response = client.post(
        "/transactions",
        data={
            "csrf_token": csrf(client, "/transactions/new"),
            "kind": "TRANSFER_IN",
            "account_id": str(account.id),
            "asset_id": "",
            "amount": "10",
            "quantity": "",
            "price": "",
            "fee": "0",
            "occurred_at": "2026-09-23T15:30",
            "note": "",
        },
    )
    assert response.status_code == 303
    db_session.expire_all()
    original = db_session.scalar(
        select(Transaction).where(Transaction.reverses_transaction_id.is_(None))
    )
    assert original is not None
    return original


@pytest.mark.parametrize(
    "value",
    ["1e2", "1.001", "NaN", "1,000", "", True, "-1", "+1", " 1.00 "],
)
def test_exact_yuan_parser_rejects_ambiguous_values(value):
    with pytest.raises(FormError):
        parse_yuan(value, "金额")


@pytest.mark.parametrize(
    ("value", "expected"),
    [("0", 0), ("0.1", 10), ("12.34", 1234), ("999999.99", 99_999_999)],
)
def test_exact_yuan_parser_converts_without_rounding(value, expected):
    assert parse_yuan(value, "金额") == expected


@pytest.mark.parametrize("value", ["1e2", "0", "-1", "1.123456789", "NaN"])
def test_positive_decimal_parser_rejects_invalid_values(value):
    with pytest.raises(FormError):
        parse_decimal(value, "价格", positive=True)


@pytest.mark.parametrize(
    "path", ["/transactions", "/transactions/new", "/accounts", "/prices/new"]
)
def test_manual_pages_require_login_and_render_after_login(client, path):
    response = client.get(path)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert login(client).status_code == 303
    response = client.get(path)
    assert response.status_code == 200
    assert 'name="viewport"' in response.text


def test_create_account_is_exact_audited_and_uses_prg(client, db_session):
    assert login(client).status_code == 303
    token = csrf(client, "/accounts")
    response = client.post(
        "/accounts",
        data={
            "csrf_token": token,
            "name": "日常账户",
            "kind": "cash",
            "currency": "CNY",
            "opening_balance": "1234.56",
        },
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/accounts"
    db_session.expire_all()
    account = db_session.scalar(select(Account).where(Account.name == "日常账户"))
    assert account is not None and account.opening_balance_cents == 123_456
    event = db_session.scalar(
        select(AuditEvent).where(AuditEvent.event_type == "account.created")
    )
    assert event is not None and event.entity_id == account.id
    details = json.loads(event.details_json)
    assert details["actor"]["username"] == "admin"
    assert details["action"] == "account.create"
    assert re.fullmatch(r"[0-9a-f-]{36}", details["request_id"])
    assert not ({"password", "csrf_token", "session"} & set(details))


@pytest.mark.parametrize("opening_balance", ["12.345", "1e2", "1,000", "NaN"])
def test_account_errors_are_422_retain_input_and_write_nothing(
    client, db_session, opening_balance
):
    assert login(client).status_code == 303
    response = client.post(
        "/accounts",
        data={
            "csrf_token": csrf(client, "/accounts"),
            "name": "保留这个名称",
            "kind": "cash",
            "currency": "CNY",
            "opening_balance": opening_balance,
        },
    )
    assert response.status_code == 422
    assert "保留这个名称" in response.text
    db_session.expire_all()
    assert db_session.scalar(select(Account).where(Account.name == "保留这个名称")) is None
    assert db_session.scalar(select(AuditEvent)) is None


def test_account_error_values_do_not_fill_asset_form(client):
    assert login(client).status_code == 303
    response = client.post(
        "/accounts",
        data={
            "csrf_token": csrf(client, "/accounts"),
            "name": "account-only-name",
            "kind": "cash",
            "currency": "CNY",
            "opening_balance": "invalid",
        },
    )

    assert response.status_code == 422
    assert 'value="account-only-name"' in form_markup(response.text, "/accounts")
    assert "account-only-name" not in form_markup(response.text, "/assets")


def test_asset_error_values_do_not_fill_account_form(client):
    assert login(client).status_code == 303
    response = client.post(
        "/assets",
        data={
            "csrf_token": csrf(client, "/accounts"),
            "code": "asset-only-code",
            "name": "asset-only-name",
            "risk_level": "invalid",
            "portfolio_role": "core",
        },
    )

    assert response.status_code == 422
    asset_form = form_markup(response.text, "/assets")
    assert 'value="asset-only-code"' in asset_form
    assert 'value="asset-only-name"' in asset_form
    assert "asset-only-name" not in form_markup(response.text, "/accounts")


def test_mutating_forms_require_csrf(client):
    assert login(client).status_code == 303
    requests = [
        ("/accounts", {"name": "x", "kind": "cash", "currency": "CNY", "opening_balance": "0"}),
        ("/assets", {"code": "1", "name": "x", "risk_level": "low", "portfolio_role": "core"}),
        ("/transactions", {}),
        ("/prices", {}),
        ("/alerts/999/read", {}),
        ("/transactions/999/reversal", {"reason": "错误"}),
    ]
    for path, data in requests:
        assert client.post(path, data=data).status_code == 403


def test_create_asset_enables_transaction_workflow_and_is_audited(client, db_session):
    assert login(client).status_code == 303
    response = client.post(
        "/assets",
        data={
            "csrf_token": csrf(client, "/accounts"),
            "code": " 161725 ",
            "name": "招商中证白酒",
            "risk_level": "high",
            "portfolio_role": "satellite",
        },
    )
    assert response.status_code == 303
    db_session.expire_all()
    asset = db_session.scalar(select(Asset).where(Asset.code == "161725"))
    assert asset is not None
    assert asset.market == "CN" and asset.currency == "CNY"
    assert asset.risk_level == "high" and asset.portfolio_role == "satellite"
    assert db_session.scalar(
        select(AuditEvent).where(AuditEvent.event_type == "asset.created")
    )


def test_post_transaction_uses_manual_uuid_and_beijing_time(client, db_session):
    account = add_account(db_session)
    asset = add_asset(db_session)
    assert login(client).status_code == 303
    response = client.post(
        "/transactions",
        data={
            "csrf_token": csrf(client, "/transactions/new"),
            "kind": "BUY",
            "account_id": str(account.id),
            "asset_id": str(asset.id),
            "amount": "100.00",
            "quantity": "10.12345678",
            "price": "9.87",
            "fee": "0.25",
            "occurred_at": "2026-09-23T15:30",
            "note": "定投",
        },
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/transactions"
    db_session.expire_all()
    transaction = db_session.scalar(select(Transaction))
    assert transaction is not None
    assert transaction.source == "manual"
    assert re.fullmatch(r"[0-9a-f-]{36}", transaction.external_id)
    assert transaction.occurred_at == datetime(2026, 9, 23, 7, 30, tzinfo=UTC)
    assert transaction.quantity == Decimal("10.12345678")
    assert transaction.amount_cents == 10_000 and transaction.fee_cents == 25
    events = list(db_session.scalars(select(AuditEvent).order_by(AuditEvent.id)))
    assert [event.event_type for event in events] == [
        "transaction.posted",
        "transaction.created",
    ]
    web_details = json.loads(events[1].details_json)
    assert web_details["actor"]["username"] == "admin"
    assert "定投" not in events[1].details_json
    service_details = json.loads(events[0].details_json)
    assert "note" not in service_details
    assert "定投" not in service_details.values()

    page = client.get("/transactions")
    assert "2026-09-23 15:30" in page.text


def test_transaction_time_formatter_explicitly_uses_shanghai_timezone():
    timestamp = datetime(2026, 9, 23, 7, 30, tzinfo=UTC)

    assert web_routes.shanghai_datetime(timestamp) == "2026-09-23 15:30"


def test_mobile_navigation_links_fund_data_center(client):
    assert login(client).status_code == 303
    page = client.get("/")
    mobile_nav = page.text.split('class="mobile-nav"', 1)[1].split("</nav>", 1)[0]
    assert 'href="/funds"' in mobile_nav
    assert "基金" in mobile_nav


def test_mobile_navigation_uses_five_shrinkable_equal_columns():
    css = Path("finance_app/static/app.css").read_text(encoding="utf-8")
    mobile_css = css.split("@media(max-width:760px)", 1)[1].split(
        "@media(prefers-reduced-motion:reduce)", 1
    )[0]
    nav_rule = re.search(r"\.mobile-nav\{([^}]*)\}", mobile_css)
    link_rule = re.search(r"\.mobile-nav a\{([^}]*)\}", mobile_css)

    assert nav_rule is not None
    assert link_rule is not None
    nav_declarations = dict(
        declaration.split(":", 1) for declaration in nav_rule.group(1).split(";")
    )
    link_declarations = dict(
        declaration.split(":", 1) for declaration in link_rule.group(1).split(";")
    )
    assert nav_declarations["display"] == "flex"
    assert link_declarations["flex"] == "1 1 0"
    assert link_declarations["min-width"] == "0"


def test_transaction_failure_rolls_back_service_and_web_audit(client, db_session):
    account = add_account(db_session, opening_balance_cents=500)
    asset = add_asset(db_session)
    assert login(client).status_code == 303
    response = client.post(
        "/transactions",
        data={
            "csrf_token": csrf(client, "/transactions/new"),
            "kind": "BUY",
            "account_id": str(account.id),
            "asset_id": str(asset.id),
            "amount": "10.00",
            "quantity": "1",
            "price": "10",
            "fee": "0",
            "occurred_at": "2026-09-23T15:30",
            "note": "must roll back",
        },
    )
    assert response.status_code == 422
    assert "must roll back" in response.text
    db_session.expire_all()
    assert db_session.scalar(select(Transaction)) is None
    assert db_session.scalar(select(AuditEvent)) is None


@pytest.mark.parametrize(
    ("account_id", "asset_id"), [("999999", ""), ("999999", "999998")]
)
def test_transaction_rejects_unknown_entity_ids(client, db_session, account_id, asset_id):
    assert login(client).status_code == 303
    response = client.post(
        "/transactions",
        data={
            "csrf_token": csrf(client, "/transactions/new"),
            "kind": "TRANSFER_IN",
            "account_id": account_id,
            "asset_id": asset_id,
            "amount": "1.00",
            "quantity": "",
            "price": "",
            "fee": "0",
            "occurred_at": "2026-09-23T15:30",
            "note": "",
        },
    )
    assert response.status_code == 422
    db_session.expire_all()
    assert db_session.scalar(select(Transaction)) is None


def test_reversal_requires_reason_and_appends_two_audit_meanings(client, db_session):
    account = add_account(db_session)
    assert login(client).status_code == 303
    token = csrf(client, "/transactions/new")
    response = client.post(
        "/transactions",
        data={
            "csrf_token": token,
            "kind": "TRANSFER_IN",
            "account_id": str(account.id),
            "asset_id": "",
            "amount": "10",
            "quantity": "",
            "price": "",
            "fee": "0",
            "occurred_at": "2026-09-23T15:30",
            "note": "",
        },
    )
    assert response.status_code == 303
    db_session.expire_all()
    original = db_session.scalar(select(Transaction))
    assert original is not None
    token = csrf(client, "/transactions")
    invalid_response = client.post(
        f"/transactions/{original.id}/reversal",
        data={"csrf_token": token, "reason": "   "},
    )
    assert invalid_response.status_code == 422
    assert 'role="alert"' in invalid_response.text
    assert "冲正原因不能为空，且首尾不能有空格。" in invalid_response.text
    response = client.post(
        f"/transactions/{original.id}/reversal",
        data={"csrf_token": token, "reason": "重复录入"},
    )
    assert response.status_code == 303
    db_session.expire_all()
    reversed_row = db_session.scalar(
        select(Transaction).where(Transaction.reverses_transaction_id == original.id)
    )
    assert reversed_row is not None
    event_types = list(db_session.scalars(select(AuditEvent.event_type)))
    assert "transaction.reversed" in event_types
    assert "transaction.reversal_requested" in event_types


def test_duplicate_reversal_error_is_visible_and_rolls_back(client, db_session):
    account = add_account(db_session)
    original = post_transfer_in(client, db_session, account)
    token = csrf(client, "/transactions")
    assert client.post(
        f"/transactions/{original.id}/reversal",
        data={"csrf_token": token, "reason": "首次冲正"},
    ).status_code == 303
    db_session.expire_all()
    transaction_count = len(db_session.scalars(select(Transaction)).all())
    audit_count = len(db_session.scalars(select(AuditEvent)).all())

    response = client.post(
        f"/transactions/{original.id}/reversal",
        data={"csrf_token": token, "reason": "再次冲正"},
    )

    assert response.status_code == 422
    assert 'role="alert"' in response.text
    assert "transaction is already reversed" in response.text
    db_session.expire_all()
    assert len(db_session.scalars(select(Transaction)).all()) == transaction_count
    assert len(db_session.scalars(select(AuditEvent)).all()) == audit_count


def test_reversing_a_reversal_error_is_visible_and_rolls_back(client, db_session):
    account = add_account(db_session)
    original = post_transfer_in(client, db_session, account)
    token = csrf(client, "/transactions")
    assert client.post(
        f"/transactions/{original.id}/reversal",
        data={"csrf_token": token, "reason": "首次冲正"},
    ).status_code == 303
    db_session.expire_all()
    reversal = db_session.scalar(
        select(Transaction).where(Transaction.reverses_transaction_id == original.id)
    )
    assert reversal is not None
    transaction_count = len(db_session.scalars(select(Transaction)).all())
    audit_count = len(db_session.scalars(select(AuditEvent)).all())

    response = client.post(
        f"/transactions/{reversal.id}/reversal",
        data={"csrf_token": token, "reason": "非法冲正"},
    )

    assert response.status_code == 422
    assert 'role="alert"' in response.text
    assert "original transaction does not exist or is a reversal" in response.text
    db_session.expire_all()
    assert len(db_session.scalars(select(Transaction)).all()) == transaction_count
    assert len(db_session.scalars(select(AuditEvent)).all()) == audit_count


def test_reversal_of_missing_transaction_remains_404(client):
    assert login(client).status_code == 303

    response = client.post(
        "/transactions/999999/reversal",
        data={"csrf_token": csrf(client, "/transactions"), "reason": "找不到原交易"},
    )

    assert response.status_code == 404


def test_manual_price_is_audited_idempotent_and_can_be_edited(client, db_session):
    asset = add_asset(db_session)
    assert login(client).status_code == 303
    data = {
        "csrf_token": csrf(client, "/prices/new"),
        "asset_id": str(asset.id),
        "valuation_date": "2026-09-23",
        "price": "1.23456789",
        "source": "fund-statement",
    }
    first = client.post("/prices", data=data)
    assert first.status_code == 303
    assert first.headers["location"] == "/prices/new"
    second = client.post("/prices", data=data)
    assert second.status_code == 303
    snapshot = db_session.scalar(select(PriceSnapshot))
    edit_page = client.get(f"/prices/{snapshot.id}/edit")
    assert edit_page.status_code == 200
    changed = {
        **data,
        "price": "1.25",
        "csrf_token": csrf(client, f"/prices/{snapshot.id}/edit"),
    }
    updated = client.post(f"/prices/{snapshot.id}", data=changed)
    assert updated.status_code == 303
    db_session.expire_all()
    prices = list(db_session.scalars(select(PriceSnapshot)))
    assert len(prices) == 1
    assert prices[0].source == "manual:fund-statement"
    assert prices[0].price == Decimal("1.25")
    events = list(
        db_session.scalars(
            select(AuditEvent).where(AuditEvent.event_type == "price.created")
        )
    )
    assert len(events) == 1
    updated_event = db_session.scalar(
        select(AuditEvent).where(AuditEvent.event_type == "price.updated")
    )
    assert updated_event is not None
    details = json.loads(updated_event.details_json)
    assert details["summary"]["old"]["price"] == "1.23456789"
    assert details["summary"]["new"]["price"] == "1.25"


def test_editing_current_nav_refreshes_existing_portfolio_snapshot(
    client, db_session, monkeypatch
):
    from datetime import UTC, datetime

    now = datetime(2026, 9, 23, 14, tzinfo=UTC)
    monkeypatch.setattr(web_routes, "utc_now", lambda: now)
    account = add_account(db_session, opening_balance_cents=1000)
    asset = add_asset(db_session)
    db_session.add(
        Holding(
            account_id=account.id,
            asset_id=asset.id,
            quantity=Decimal(10),
            cost_cents=1000,
        )
    )
    db_session.add(
        CashBucket(bucket_kind="reserve", balance_cents=100000, updated_at=now)
    )
    db_session.add(
        PriceSnapshot(
            asset_id=asset.id,
            valuation_date=now.date(),
            price=Decimal(1),
            source="manual:fund-statement",
            fetched_at=now,
        )
    )
    db_session.flush()
    create_daily_snapshot(
        db_session,
        now=now,
        holdings_confirmed_at=now,
        cash_confirmed_at=now,
    )
    db_session.commit()

    assert login(client).status_code == 303
    response = client.post(
        f"/prices/{db_session.scalar(select(PriceSnapshot)).id}",
        data={
            "csrf_token": csrf(client, "/prices/new"),
            "asset_id": str(asset.id),
            "valuation_date": now.date().isoformat(),
            "price": "2",
            "source": "fund-statement",
        },
    )
    assert response.status_code == 303
    db_session.expire_all()
    refreshed = db_session.scalar(select(PortfolioSnapshot))
    assert refreshed is not None
    assert refreshed.data_complete is True
    assert refreshed.total_value_cents == 3000


@pytest.mark.parametrize(
    ("valuation_date", "price", "source"),
    [
        ("2999-01-01", "1", "entry"),
        ("2026-09-23", "0", "entry"),
        ("2026-09-23", "1.123456789", "entry"),
        ("2026-09-23", "1", "realtime"),
        ("2026-09-23", "1", "manual:"),
    ],
)
def test_manual_price_rejects_invalid_date_price_or_source(
    client, db_session, valuation_date, price, source
):
    asset = add_asset(db_session)
    assert login(client).status_code == 303
    response = client.post(
        "/prices",
        data={
            "csrf_token": csrf(client, "/prices/new"),
            "asset_id": str(asset.id),
            "valuation_date": valuation_date,
            "price": price,
            "source": source,
        },
    )
    assert response.status_code == 422
    db_session.expire_all()
    assert db_session.scalar(select(PriceSnapshot)) is None


def test_price_blank_source_defaults_to_user_entry(client, db_session):
    asset = add_asset(db_session)
    assert login(client).status_code == 303
    response = client.post(
        "/prices",
        data={
            "csrf_token": csrf(client, "/prices/new"),
            "asset_id": str(asset.id),
            "valuation_date": "2026-09-23",
            "price": "1",
            "source": "",
        },
    )
    assert response.status_code == 303
    db_session.expire_all()
    assert db_session.scalar(select(PriceSnapshot.source)) == "manual:user-entry"


def test_mark_alert_read_is_csrf_protected_audited_and_idempotent(client, db_session):
    alert = Alert(alert_type="price", severity="warning", message="净值待确认")
    db_session.add(alert)
    db_session.commit()
    assert login(client).status_code == 303
    assert client.post(f"/alerts/{alert.id}/read").status_code == 403
    token = csrf(client, "/alerts")
    response = client.post(
        f"/alerts/{alert.id}/read", data={"csrf_token": token}
    )
    assert response.status_code == 303
    second = client.post(f"/alerts/{alert.id}/read", data={"csrf_token": token})
    assert second.status_code == 303
    db_session.expire_all()
    db_session.refresh(alert)
    assert alert.status == "read"
    events = list(
        db_session.scalars(
            select(AuditEvent).where(AuditEvent.event_type == "alert.read")
        )
    )
    assert len(events) == 1
    assert client.post(
        "/alerts/999999/read", data={"csrf_token": token}
    ).status_code == 404


def test_fund_center_requires_login_and_reads_only_local_data(
    client, db_session, monkeypatch
):
    asset = add_held_fund(db_session)
    calls: list[tuple[int | None, str]] = []

    def fake_build(db, *, selected_asset_id, period, now):
        assert db is not None
        assert now.tzinfo is not None
        calls.append((selected_asset_id, period))
        return {"selected": {"asset_id": selected_asset_id}, "funds": []}

    def external_call_is_a_bug(*args, **kwargs):
        raise AssertionError("GET /funds must not construct an external provider")

    monkeypatch.setattr(web_routes, "build_fund_center", fake_build)
    monkeypatch.setattr(web_routes, "EastMoneyFundNavProvider", external_call_is_a_bug)
    monkeypatch.setattr(web_routes, "EfinanceAdapter", external_call_is_a_bug)

    response = client.get("/funds")
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert login(client).status_code == 303
    response = client.get(f"/funds?tab=risk&asset_id={asset.id}&period=3m")
    assert response.status_code == 200
    assert calls == [(asset.id, "3m")]


def test_fund_center_invalid_tab_and_period_fall_back_to_defaults(
    client, db_session, monkeypatch
):
    add_held_fund(db_session)
    captured: dict[str, object] = {}

    def fake_build(db, *, selected_asset_id, period, now):
        captured["period"] = period
        return {"selected": {"asset_id": None}, "funds": []}

    original_response = web_routes.templates.TemplateResponse

    def capture_response(*, request, name, context, status_code=200):
        if "fund_tab" in context:
            captured["name"] = name
            captured["tab"] = context["fund_tab"]
        return original_response(
            request=request, name=name, context=context, status_code=status_code
        )

    monkeypatch.setattr(web_routes, "build_fund_center", fake_build)
    monkeypatch.setattr(web_routes.templates, "TemplateResponse", capture_response)
    assert login(client).status_code == 303

    response = client.get("/funds?tab=invalid&period=invalid")

    assert response.status_code == 200
    assert captured == {
        "period": "1y",
        "name": "price_form.html",
        "tab": "quotes",
    }


def test_price_pages_use_manual_fund_context(client, db_session, monkeypatch):
    asset = add_held_fund(db_session)
    snapshot = PriceSnapshot(
        asset_id=asset.id,
        valuation_date=date(2026, 9, 29),
        price=Decimal("1.1"),
        source="manual:user-entry",
        fetched_at=datetime(2026, 9, 29, 8, tzinfo=UTC),
    )
    db_session.add(snapshot)
    db_session.commit()
    calls: list[int | None] = []

    def fake_build(db, *, selected_asset_id, period, now):
        calls.append(selected_asset_id)
        return {"selected": {"asset_id": selected_asset_id}, "funds": []}

    monkeypatch.setattr(web_routes, "build_fund_center", fake_build)
    assert login(client).status_code == 303

    assert client.get("/prices/new").status_code == 200
    assert client.get(f"/prices/{snapshot.id}/edit").status_code == 200
    assert calls == [None, asset.id]


def test_fund_center_renders_four_fund_only_views(client, db_session, monkeypatch):
    now = datetime(2026, 9, 30, 4, 0, tzinfo=UTC)
    monkeypatch.setattr(web_routes, "utc_now", lambda: now)
    asset = add_held_fund(db_session)
    db_session.add_all(
        [
            PriceSnapshot(
                asset_id=asset.id,
                valuation_date=date(2026, 8, 30),
                price=Decimal("1.00"),
                source="efinance",
                source_url="https://example.test/history",
                fetched_at=datetime(2026, 8, 30, 8, tzinfo=UTC),
            ),
            PriceSnapshot(
                asset_id=asset.id,
                valuation_date=date(2026, 9, 29),
                price=Decimal("1.20"),
                source="eastmoney",
                source_url="https://example.test/nav",
                fetched_at=datetime(2026, 9, 29, 8, tzinfo=UTC),
            ),
            PriceSnapshot(
                asset_id=asset.id,
                valuation_date=date(2026, 9, 30),
                price=Decimal("1.23"),
                source="tiantian:estimate",
                source_url="https://example.test/estimate",
                fetched_at=now,
                quote_type=QuoteType.INTRADAY_ESTIMATE.value,
            ),
        ]
    )
    db_session.commit()
    assert login(client).status_code == 303

    quotes = client.get(f"/funds?asset_id={asset.id}&period=1y")
    risk = client.get(f"/funds?tab=risk&asset_id={asset.id}&period=1y")
    holdings = client.get(
        f"/funds?tab=holdings&asset_id={asset.id}&period=1y"
    )

    assert quotes.status_code == risk.status_code == holdings.status_code == 200
    for label in ("行情", "风险分析", "持仓跟踪", "手工净值"):
        assert label in quotes.text
    assert 'data-active-tab="quotes"' in quotes.text
    for label in (
        "最新官方净值",
        "官方净值日期",
        "数据来源",
        "最近获取时间",
        "获取最新官方净值",
    ):
        assert label in quotes.text
    assert 'viewBox="0 0 600 220"' in quotes.text
    assert f'action="/funds/{asset.id}/refresh"' in quotes.text
    assert 'name="csrf_token"' in form_markup(
        quotes.text, f"/funds/{asset.id}/refresh"
    )
    for period in ("1m", "3m", "6m", "1y", "all"):
        assert f"period={period}" in quotes.text
    assert "区间收益率" in risk.text
    assert "最大回撤" in risk.text
    assert "年化波动率" in risk.text
    assert "风险等级" in risk.text
    assert "组合占比" in holdings.text
    assert "持仓收益率" in holdings.text
    active_ui = quotes.text + risk.text + holdings.text
    for forbidden in ("盘中估值", "估算涨跌", "估算盈亏", "刷新行情"):
        assert forbidden not in active_ui
    for forbidden in ("重仓股", "股票", "确认陈旧"):
        assert forbidden not in quotes.text + risk.text + holdings.text


def test_fund_center_missing_metrics_and_refresh_result_are_compact(
    client, db_session
):
    asset = add_held_fund(db_session)
    assert login(client).status_code == 303

    missing = client.get(f"/funds?tab=risk&asset_id={asset.id}")
    success = client.get(f"/funds?asset_id={asset.id}&refresh=success")
    failed = client.get(f"/funds?asset_id={asset.id}&refresh=failed")
    refreshed = client.get(f"/funds?asset_id={asset.id}&refresh=cooldown")

    assert missing.text.count("暂无数据") >= 4
    assert "已获取上游最新官方净值，实际净值日期见下方。" in success.text
    assert "暂时未获取到官方净值，已保留最近有效数据。" in failed.text
    assert 'role="status"' in refreshed.text
    assert "刷新过于频繁" in refreshed.text


def test_holding_table_labels_official_nav_status_only():
    template = Path("finance_app/templates/holding_table.html").read_text(
        encoding="utf-8"
    )

    assert "官方净值日期 {{ h.date }}" in template
    assert "暂无官方净值" in template
    assert "h.estimate" not in template


def test_manual_tab_has_quick_summary_edit_and_delete_controls(client, db_session):
    asset = add_held_fund(db_session)
    row = PriceSnapshot(
        asset_id=asset.id,
        valuation_date=date(2026, 9, 29),
        price=Decimal("1.2"),
        source="manual:statement",
        fetched_at=datetime(2026, 9, 29, 8, tzinfo=UTC),
    )
    db_session.add(row)
    db_session.commit()
    assert login(client).status_code == 303

    response = client.get(f"/prices/{row.id}/edit")

    assert response.status_code == 200
    assert 'data-active-tab="manual"' in response.text
    assert "当前基金摘要" in response.text
    assert f'href="/prices/{row.id}/edit"' in response.text
    delete_form = form_markup(response.text, f"/prices/{row.id}/delete")
    assert 'name="csrf_token"' in delete_form
    assert "confirm('确认删除这条手工净值？')" in delete_form


def test_fund_center_css_has_stable_responsive_layout():
    css = Path("finance_app/static/forms.css").read_text(encoding="utf-8")

    assert ".fund-center-grid" in css
    assert "grid-template-columns:minmax(12rem,18rem) minmax(0,1fr)" in css
    assert ".fund-chart" in css
    assert "aspect-ratio:30/11" in css
    assert ".fund-metrics" in css
    assert "repeat(3,minmax(0,1fr))" in css
    assert ".quote-estimate" not in css
    mobile_css = css.split("@media(max-width:760px)", 1)[1]
    assert ".fund-center-grid,.fund-metrics{grid-template-columns:1fr}" in mobile_css
    assert ".fund-tabs{overflow-x:auto}" in mobile_css


def test_manual_official_nav_refresh_is_cooled_down_and_audited(
    client, db_session, monkeypatch
):
    now = datetime(2026, 9, 30, 4, 0, tzinfo=UTC)
    asset = add_held_fund(db_session)
    provider_calls: list[str] = []
    closed: list[bool] = []

    class Primary:
        source = "eastmoney"
        source_url = "https://api.fund.eastmoney.com/f10/lsjz"

        def __init__(self, *, clock):
            self.clock = clock

        def fetch(self, code):
            provider_calls.append(code)
            return FundNavQuote(
                value=Decimal("1.23"),
                valuation_date=date(2026, 9, 29),
                source=self.source,
                source_url=self.source_url,
                fetched_at=now,
                quote_type=QuoteType.OFFICIAL_NAV,
            )

        def close(self):
            closed.append(True)

    class Fallback:
        source = "efinance"
        source_url = "https://example.test/efinance"

        def __init__(self, *, clock):
            self.clock = clock

        def fetch(self, code):
            raise AssertionError("fallback must not run after primary success")

    monkeypatch.setattr(web_routes, "utc_now", lambda: now)
    monkeypatch.setattr(web_routes, "EastMoneyFundNavProvider", Primary)
    monkeypatch.setattr(web_routes, "EfinanceAdapter", Fallback)
    assert login(client).status_code == 303
    token = csrf(client, f"/funds?asset_id={asset.id}")

    first = client.post(
        f"/funds/{asset.id}/refresh", data={"csrf_token": token}
    )
    second = client.post(
        f"/funds/{asset.id}/refresh", data={"csrf_token": token}
    )

    assert first.status_code == 303
    assert first.headers["location"] == f"/funds?asset_id={asset.id}&refresh=success"
    assert second.status_code == 303
    assert second.headers["location"] == f"/funds?asset_id={asset.id}&refresh=cooldown"
    assert provider_calls == [asset.code]
    assert closed == [True]
    db_session.expire_all()
    official_nav = db_session.scalar(
        select(PriceSnapshot).where(
            PriceSnapshot.quote_type == QuoteType.OFFICIAL_NAV.value
        )
    )
    assert official_nav is not None
    events = list(
        db_session.scalars(
            select(AuditEvent).where(AuditEvent.event_type == "fund.refresh_requested")
        )
    )
    assert len(events) == 1
    details = json.loads(events[0].details_json)
    assert details["summary"] == {
        "status": "success",
        "source": "eastmoney",
        "attempts": 1,
        "error": None,
    }


def test_failed_manual_refresh_uses_fallback_and_still_activates_cooldown(
    client, db_session, monkeypatch
):
    now = datetime(2026, 9, 30, 4, 0, tzinfo=UTC)
    asset = add_held_fund(db_session)
    provider_calls: list[str] = []
    closed: list[bool] = []

    class Primary:
        source = "eastmoney"
        source_url = "https://api.fund.eastmoney.com/f10/lsjz"

        def __init__(self, *, clock):
            pass

        def fetch(self, code):
            provider_calls.append("primary")
            raise RuntimeError("upstream private body must not be audited")

        def close(self):
            closed.append(True)

    class Fallback:
        source = "efinance"
        source_url = "https://example.test/efinance"

        def __init__(self, *, clock):
            pass

        def fetch(self, code):
            provider_calls.append("fallback")
            raise RuntimeError("fallback private body must not be audited")

    monkeypatch.setattr(web_routes, "utc_now", lambda: now)
    monkeypatch.setattr(web_routes, "EastMoneyFundNavProvider", Primary)
    monkeypatch.setattr(web_routes, "EfinanceAdapter", Fallback)
    assert login(client).status_code == 303
    token = csrf(client, f"/funds?asset_id={asset.id}")

    first = client.post(
        f"/funds/{asset.id}/refresh", data={"csrf_token": token}
    )
    second = client.post(
        f"/funds/{asset.id}/refresh", data={"csrf_token": token}
    )

    assert first.headers["location"] == f"/funds?asset_id={asset.id}&refresh=failed"
    assert second.headers["location"] == f"/funds?asset_id={asset.id}&refresh=cooldown"
    assert provider_calls == ["primary", "fallback"]
    assert closed == [True]
    event = db_session.scalar(
        select(AuditEvent).where(AuditEvent.event_type == "fund.refresh_requested")
    )
    assert event is not None
    details = json.loads(event.details_json)
    assert details["summary"]["status"] == "failed"
    assert details["summary"]["error"] == "official_nav_unavailable"
    assert "private body" not in event.details_json


@pytest.mark.parametrize(
    ("market", "asset_class", "quantity"),
    [("HK", "fund", "1"), ("CN", "stock", "1"), ("CN", "fund", "0")],
)
def test_manual_refresh_requires_currently_held_cn_fund(
    client, db_session, monkeypatch, market, asset_class, quantity
):
    account = Account(name="account", kind="investment", currency="CNY")
    asset = Asset(
        code="000001",
        market=market,
        name="not-eligible",
        asset_class=asset_class,
        currency="CNY",
    )
    db_session.add_all([account, asset])
    db_session.flush()
    db_session.add(
        Holding(
            account_id=account.id,
            asset_id=asset.id,
            quantity=Decimal(quantity),
            cost_cents=100,
        )
    )
    db_session.commit()
    monkeypatch.setattr(
        web_routes,
        "EastMoneyFundNavProvider",
        lambda **kwargs: pytest.fail("provider must not be constructed"),
    )
    assert login(client).status_code == 303

    response = client.post(
        f"/funds/{asset.id}/refresh",
        data={"csrf_token": csrf(client, "/prices/new")},
    )

    assert response.status_code == 404


def test_manual_price_delete_is_csrf_protected_manual_only_and_audited(
    client, db_session
):
    asset = add_asset(db_session)
    manual = PriceSnapshot(
        asset_id=asset.id,
        valuation_date=date(2026, 9, 28),
        price=Decimal("1.23456789"),
        source="manual:statement",
        fetched_at=datetime(2026, 9, 28, 8, tzinfo=UTC),
    )
    automatic = PriceSnapshot(
        asset_id=asset.id,
        valuation_date=date(2026, 9, 29),
        price=Decimal("1.25"),
        source="eastmoney",
        fetched_at=datetime(2026, 9, 29, 8, tzinfo=UTC),
    )
    db_session.add_all([manual, automatic])
    db_session.commit()
    manual_id = manual.id
    automatic_id = automatic.id
    assert login(client).status_code == 303

    assert client.post(f"/prices/{manual_id}/delete").status_code == 403
    token = csrf(client, "/prices/new")
    assert client.post(
        f"/prices/{automatic_id}/delete", data={"csrf_token": token}
    ).status_code == 403
    response = client.post(
        f"/prices/{manual_id}/delete", data={"csrf_token": token}
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/prices/new"
    db_session.expire_all()
    assert db_session.get(PriceSnapshot, manual_id) is None
    assert db_session.get(PriceSnapshot, automatic_id) is not None
    event = db_session.scalar(
        select(AuditEvent).where(AuditEvent.event_type == "price.deleted")
    )
    assert event is not None
    details = json.loads(event.details_json)
    assert details["summary"] == {
        "old": {
            "asset_id": asset.id,
            "valuation_date": "2026-09-28",
            "price": "1.23456789",
            "source": "manual:statement",
        }
    }
    assert client.post(
        "/prices/999999/delete", data={"csrf_token": token}
    ).status_code == 404


def test_deleting_current_manual_nav_refreshes_portfolio_snapshot(
    client, db_session, monkeypatch
):
    now = datetime(2026, 9, 30, 4, 0, tzinfo=UTC)
    monkeypatch.setattr(web_routes, "utc_now", lambda: now)
    account = add_account(db_session, opening_balance_cents=1_000)
    asset = add_asset(db_session)
    asset.asset_class = "fund"
    db_session.add(
        Holding(
            account_id=account.id,
            asset_id=asset.id,
            quantity=Decimal(10),
            cost_cents=1_000,
        )
    )
    db_session.add(CashBucket(bucket_kind="reserve", balance_cents=100_000))
    price = PriceSnapshot(
        asset_id=asset.id,
        valuation_date=now.astimezone(web_routes.SHANGHAI).date(),
        price=Decimal(2),
        source="manual:statement",
        fetched_at=now,
    )
    db_session.add(price)
    db_session.flush()
    create_daily_snapshot(
        db_session,
        now=now,
        holdings_confirmed_at=now,
        cash_confirmed_at=now,
    )
    db_session.commit()
    snapshot_id = price.id
    assert db_session.scalar(select(PortfolioSnapshot)).total_value_cents == 3_000
    assert login(client).status_code == 303

    response = client.post(
        f"/prices/{snapshot_id}/delete",
        data={"csrf_token": csrf(client, "/prices/new")},
    )

    assert response.status_code == 303
    db_session.expire_all()
    refreshed = db_session.scalar(select(PortfolioSnapshot))
    assert refreshed is not None
    assert refreshed.data_complete is False
    assert refreshed.total_value_cents is None
