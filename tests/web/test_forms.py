from __future__ import annotations

import json
import re
from datetime import UTC, datetime
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
from finance_app.ledger.models import Account, Asset, AuditEvent, Transaction
from finance_app.portfolio.models import Alert, PriceSnapshot
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


def test_mobile_navigation_links_manual_price_entry(client):
    assert login(client).status_code == 303
    page = client.get("/")
    mobile_nav = page.text.split('class="mobile-nav"', 1)[1].split("</nav>", 1)[0]
    assert 'href="/prices/new"' in mobile_nav
    assert "净值" in mobile_nav


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


def test_manual_price_is_audited_idempotent_and_conflicts_on_change(client, db_session):
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
    changed = {**data, "price": "1.25"}
    conflict = client.post("/prices", data=changed)
    assert conflict.status_code == 422
    db_session.expire_all()
    prices = list(db_session.scalars(select(PriceSnapshot)))
    assert len(prices) == 1
    assert prices[0].source == "manual:fund-statement"
    assert prices[0].price == Decimal("1.23456789")
    events = list(
        db_session.scalars(
            select(AuditEvent).where(AuditEvent.event_type == "price.created")
        )
    )
    assert len(events) == 1


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
