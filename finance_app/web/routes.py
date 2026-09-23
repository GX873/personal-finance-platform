from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from finance_app.auth.models import User
from finance_app.auth.routes import templates
from finance_app.auth.service import (
    csrf_token,
    current_user,
    require_csrf,
    require_user,
)
from finance_app.config import get_settings
from finance_app.db import get_db, utc_now
from finance_app.ledger.models import Account, Asset, Transaction
from finance_app.ledger.schemas import PostTransaction
from finance_app.ledger.service import post_transaction, reverse_transaction
from finance_app.portfolio.models import Alert, PriceSnapshot
from finance_app.web.forms import (
    SHANGHAI,
    FormError,
    audit_event,
    manual_source,
    parse_date,
    parse_decimal,
    parse_id,
    parse_local_datetime,
    parse_yuan,
    required_text,
)
from finance_app.web.viewmodels import dashboard, money

router = APIRouter()


def shanghai_datetime(value: datetime) -> str:
    return value.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M")


templates.env.filters["money"] = money
templates.env.filters["shanghai_datetime"] = shanghai_datetime


def _today() -> str:
    return utc_now().astimezone(SHANGHAI).date().isoformat()


def _base_context(request: Request, user: User, section: str) -> dict[str, Any]:
    return {
        "user": user,
        "csrf_token": csrf_token(request),
        "section": section,
        "demo": False,
        "vm": {"today": _today()},
    }


def _form_values(form: Any) -> dict[str, str]:
    return {key: value for key, value in form.items() if isinstance(value, str)}


def _manual_page(
    request: Request,
    db: Session,
    user: User,
    *,
    name: str,
    section: str,
    status_code: int = 200,
    error: str | None = None,
    values: dict[str, str] | None = None,
):
    context = _base_context(request, user, section)
    context.update(
        {
            "accounts": list(db.scalars(select(Account).order_by(Account.name))),
            "assets": list(db.scalars(select(Asset).order_by(Asset.code))),
            "error": error,
            "values": values or {},
            "local_now": datetime.now(SHANGHAI).strftime("%Y-%m-%dT%H:%M"),
        }
    )
    return templates.TemplateResponse(
        request=request, name=name, context=context, status_code=status_code
    )


@router.get("/", response_class=HTMLResponse)
@router.get("/holdings", response_class=HTMLResponse)
@router.get("/alerts", response_class=HTMLResponse)
def page(request: Request, db: Annotated[Session, Depends(get_db)]):
    user = current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    section = {"/": "dashboard", "/holdings": "holdings", "/alerts": "alerts"}[
        request.url.path
    ]
    return templates.TemplateResponse(
        request=request,
        name=f"{section}.html",
        context={
            "user": user,
            "csrf_token": csrf_token(request),
            "section": section,
            "demo": False,
            "vm": dashboard(db, section=section),
        },
    )


@router.get("/transactions", response_class=HTMLResponse)
def transactions_page(request: Request, db: Annotated[Session, Depends(get_db)]):
    user = current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    context = _base_context(request, user, "transactions")
    rows = list(
        db.scalars(
            select(Transaction)
            .order_by(Transaction.occurred_at.desc(), Transaction.id.desc())
            .limit(100)
        )
    )
    context.update(
        {
            "transactions": rows,
            "account_names": {
                row.id: row.name for row in db.scalars(select(Account))
            },
            "asset_names": {row.id: row.name for row in db.scalars(select(Asset))},
            "reversed_ids": {
                row.reverses_transaction_id
                for row in rows
                if row.reverses_transaction_id is not None
            },
        }
    )
    return templates.TemplateResponse(
        request=request, name="transactions.html", context=context
    )


@router.get("/transactions/new", response_class=HTMLResponse)
def transaction_form(request: Request, db: Annotated[Session, Depends(get_db)]):
    user = current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    return _manual_page(
        request, db, user, name="transaction_form.html", section="transactions"
    )


@router.get("/accounts", response_class=HTMLResponse)
def accounts_page(request: Request, db: Annotated[Session, Depends(get_db)]):
    user = current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    return _manual_page(request, db, user, name="accounts.html", section="accounts")


@router.get("/prices/new", response_class=HTMLResponse)
def price_form(request: Request, db: Annotated[Session, Depends(get_db)]):
    user = current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    return _manual_page(request, db, user, name="price_form.html", section="prices")


@router.post("/accounts", dependencies=[Depends(require_csrf)])
async def create_account(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
):
    form = await request.form()
    values = _form_values(form)
    try:
        name = required_text(form.get("name"), "账户名称")
        kind = required_text(form.get("kind"), "账户类型", maximum=50)
        if kind not in {"cash", "bank", "brokerage"}:
            raise FormError("请选择有效的账户类型。")
        currency = required_text(form.get("currency"), "币种", maximum=3).upper()
        if currency != "CNY":
            raise FormError("目前只支持人民币账户。")
        opening = parse_yuan(form.get("opening_balance"), "期初余额")
        account = Account(
            name=name,
            kind=kind,
            currency=currency,
            opening_balance_cents=opening,
        )
        db.add(account)
        db.flush()
        db.add(
            audit_event(
                user=user,
                event_type="account.created",
                action="account.create",
                entity_type="account",
                entity_id=account.id,
                summary={"name": name, "kind": kind, "currency": currency},
            )
        )
        db.commit()
    except (FormError, IntegrityError, ValueError) as exc:
        db.rollback()
        return _manual_page(
            request,
            db,
            user,
            name="accounts.html",
            section="accounts",
            status_code=422,
            error=str(exc),
            values=values,
        )
    return RedirectResponse("/accounts", status_code=303)


@router.post("/assets", dependencies=[Depends(require_csrf)])
async def create_asset(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
):
    form = await request.form()
    values = _form_values(form)
    try:
        raw_code, raw_name = form.get("code"), form.get("name")
        code = required_text(
            raw_code.strip() if isinstance(raw_code, str) else raw_code,
            "基金代码",
            maximum=64,
        )
        name = required_text(
            raw_name.strip() if isinstance(raw_name, str) else raw_name, "基金名称"
        )
        risk = required_text(form.get("risk_level"), "风险等级", maximum=16)
        role = required_text(form.get("portfolio_role"), "组合角色", maximum=16)
        if risk not in {"low", "medium", "high"}:
            raise FormError("请选择有效的风险等级。")
        if role not in {"core", "satellite"}:
            raise FormError("请选择有效的组合角色。")
        asset = Asset(
            code=code,
            market="CN",
            name=name,
            asset_class="fund",
            risk_level=risk,
            portfolio_role=role,
            currency="CNY",
        )
        db.add(asset)
        db.flush()
        db.add(
            audit_event(
                user=user,
                event_type="asset.created",
                action="asset.create",
                entity_type="asset",
                entity_id=asset.id,
                summary={"code": code, "risk_level": risk, "portfolio_role": role},
            )
        )
        db.commit()
    except (FormError, IntegrityError, ValueError) as exc:
        db.rollback()
        message = "该基金代码已经存在。" if isinstance(exc, IntegrityError) else str(exc)
        return _manual_page(
            request,
            db,
            user,
            name="accounts.html",
            section="accounts",
            status_code=422,
            error=message,
            values=values,
        )
    return RedirectResponse("/accounts", status_code=303)


@router.post("/transactions", dependencies=[Depends(require_csrf)])
async def create_transaction(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
):
    form = await request.form()
    values = _form_values(form)
    try:
        kind = required_text(form.get("kind"), "交易类型", maximum=20).upper()
        account_id = parse_id(form.get("account_id"), "账户")
        asset_id = parse_id(form.get("asset_id"), "资产", optional=True)
        if account_id is None:
            raise FormError("请选择有效的账户。")
        amount_cents = parse_yuan(form.get("amount"), "金额", positive=True)
        fee_cents = parse_yuan(form.get("fee"), "手续费")
        if kind in {"BUY", "SELL"}:
            quantity = parse_decimal(form.get("quantity"), "份额", positive=True)
            raw_price = form.get("price")
            price = (
                parse_decimal(raw_price, "成交价格", positive=True)
                if raw_price != ""
                else None
            )
        else:
            quantity, price = Decimal(0), None
        occurred_at = parse_local_datetime(form.get("occurred_at"))
        raw_note = form.get("note")
        note = required_text(raw_note, "备注", maximum=1000) if raw_note else None
        transaction = post_transaction(
            db,
            PostTransaction(
                source="manual",
                external_id=str(uuid4()),
                kind=kind,
                account_id=account_id,
                asset_id=asset_id,
                amount_cents=amount_cents,
                quantity=quantity,
                price=price,
                fee_cents=fee_cents,
                occurred_at=occurred_at,
                note=note,
            ),
        )
        db.add(
            audit_event(
                user=user,
                event_type="transaction.created",
                action="transaction.create",
                entity_type="transaction",
                entity_id=transaction.id,
                summary={
                    "kind": kind,
                    "account_id": account_id,
                    "asset_id": asset_id,
                    "amount_cents": amount_cents,
                },
            )
        )
        db.commit()
    except (FormError, IntegrityError, ValueError) as exc:
        db.rollback()
        return _manual_page(
            request,
            db,
            user,
            name="transaction_form.html",
            section="transactions",
            status_code=422,
            error=str(exc),
            values=values,
        )
    return RedirectResponse("/transactions", status_code=303)


@router.post(
    "/transactions/{transaction_id}/reversal", dependencies=[Depends(require_csrf)]
)
async def reverse_manual_transaction(
    transaction_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
):
    form = await request.form()
    try:
        reason = required_text(form.get("reason"), "冲正原因", maximum=500)
        reversal = reverse_transaction(
            db,
            transaction_id,
            reason,
            source="manual-reversal",
            external_id=str(uuid4()),
        )
        db.add(
            audit_event(
                user=user,
                event_type="transaction.reversal_requested",
                action="transaction.reverse",
                entity_type="transaction",
                entity_id=reversal.id,
                summary={"original_transaction_id": transaction_id},
            )
        )
        db.commit()
    except (FormError, IntegrityError, ValueError) as exc:
        db.rollback()
        if db.get(Transaction, transaction_id) is None:
            raise HTTPException(status_code=404, detail="Transaction not found") from exc
        response = transactions_page(request, db)
        response.status_code = 422
        return response
    return RedirectResponse("/transactions", status_code=303)


@router.post("/prices", dependencies=[Depends(require_csrf)])
async def create_price(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
):
    form = await request.form()
    values = _form_values(form)
    try:
        asset_id = parse_id(form.get("asset_id"), "资产")
        if asset_id is None or db.get(Asset, asset_id) is None:
            raise FormError("所选资产不存在。")
        valuation_date = parse_date(form.get("valuation_date"), "估值日期")
        if valuation_date > utc_now().astimezone(SHANGHAI).date():
            raise FormError("估值日期不能晚于今天。")
        price = parse_decimal(form.get("price"), "价格", positive=True)
        source = manual_source(form.get("source", ""))
        existing = db.scalar(
            select(PriceSnapshot).where(
                PriceSnapshot.asset_id == asset_id,
                PriceSnapshot.valuation_date == valuation_date,
                PriceSnapshot.source == source,
            )
        )
        if existing is not None:
            if existing.price != price:
                raise FormError("同一资产、日期和来源已经记录了不同价格。")
            return RedirectResponse("/prices/new", status_code=303)
        snapshot = PriceSnapshot(
            asset_id=asset_id,
            valuation_date=valuation_date,
            source=source,
            price=price,
            fetched_at=utc_now(),
        )
        db.add(snapshot)
        db.flush()
        db.add(
            audit_event(
                user=user,
                event_type="price.created",
                action="price.create",
                entity_type="price_snapshot",
                entity_id=snapshot.id,
                summary={
                    "asset_id": asset_id,
                    "valuation_date": valuation_date.isoformat(),
                    "source": source,
                },
            )
        )
        db.commit()
    except (FormError, IntegrityError, ValueError) as exc:
        db.rollback()
        return _manual_page(
            request,
            db,
            user,
            name="price_form.html",
            section="prices",
            status_code=422,
            error=str(exc),
            values=values,
        )
    return RedirectResponse("/prices/new", status_code=303)


@router.post("/alerts/{alert_id}/read", dependencies=[Depends(require_csrf)])
def mark_alert_read(
    alert_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
):
    alert = db.get(Alert, alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="Alert not found")
    if alert.status == "open":
        alert.status = "read"
        db.add(
            audit_event(
                user=user,
                event_type="alert.read",
                action="alert.read",
                entity_type="alert",
                entity_id=alert.id,
                summary={"previous_status": "open", "status": "read"},
            )
        )
        db.commit()
    return RedirectResponse("/alerts", status_code=303)


@router.get("/preview", response_class=HTMLResponse, include_in_schema=False)
def preview(request: Request):
    settings = get_settings()
    if not settings.demo_mode or settings.environment != "development":
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={"section": "dashboard", "demo": True, "vm": dashboard()},
    )
