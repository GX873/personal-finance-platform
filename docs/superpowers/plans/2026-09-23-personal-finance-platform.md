# Personal Finance Platform Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and deploy a single-user personal finance platform that records the user's portfolio and cash flow, produces data-aware risk suggestions, and sends email plus optional WeChat notifications without executing trades.

**Architecture:** A FastAPI monolith renders responsive Jinja pages and exposes a small JSON API. SQLAlchemy and Alembic manage a WAL-enabled SQLite database; systemd services/timers run the app, daily checks, and backups behind Nginx on the Alibaba Cloud ECS.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, Alembic, Jinja2, vanilla JavaScript/CSS, SQLite, httpx, openpyxl, Tesseract OCR, pytest, Nginx, systemd

---

## Delivery Order and File Map

The work is delivered in three independently verifiable phases:

1. **Core ledger and risk engine:** a tested local API with authentication, cash buckets, holdings, transactions, valuations, and deterministic advice.
2. **Operator interface and integrations:** responsive pages, imports, OCR confirmation, market data, alerts, email/WeChat, exports, and backups.
3. **Server deployment:** a dedicated Linux user, Nginx, systemd units/timers, smoke tests, restore test, and operating guide.

Primary files and responsibilities:

```text
pyproject.toml                         Python package and dependency metadata
.env.example                           Non-secret configuration contract
finance_app/app.py                     FastAPI application factory and middleware
finance_app/config.py                  Typed environment settings
finance_app/db.py                      Engine/session setup and SQLite pragmas
finance_app/cli.py                     init, daily-check, import, backup and restore commands
finance_app/auth/models.py             Single administrator model
finance_app/auth/service.py            Password hashing and session authentication
finance_app/auth/routes.py             Login/logout endpoints
finance_app/ledger/models.py           Accounts, assets, transactions, budgets and audit records
finance_app/ledger/service.py          Idempotent posting, reversal and holding calculations
finance_app/portfolio/models.py        Prices, targets and daily snapshots
finance_app/portfolio/rules.py         Freshness, reserve, allocation and risk rules
finance_app/portfolio/service.py       Valuation and daily snapshot orchestration
finance_app/imports/parser.py          CSV/XLSX parsing and normalization
finance_app/imports/ocr.py             Tesseract candidate extraction
finance_app/imports/routes.py          Preview/confirm import workflow
finance_app/market/eastmoney.py        Public daily fund NAV adapter
finance_app/notifications/base.py      Notification value objects and adapter protocol
finance_app/notifications/email.py     SMTP adapter
finance_app/notifications/wechat.py    PushPlus, ServerChan and WeCom adapters
finance_app/notifications/service.py   Retry and delivery audit orchestration
finance_app/web/routes.py              Authenticated pages and form actions
finance_app/web/viewmodels.py          Dashboard/analysis presentation data
finance_app/templates/                 Server-rendered HTML templates
finance_app/static/app.css             Responsive visual system
finance_app/static/app.js              Confirmation and chart helpers
finance_app/ops/backup.py              SQLite online backup, retention and restore verification
deploy/finance-app.service             Local-only application service
deploy/finance-daily.service           One-shot daily check
deploy/finance-daily.timer             09:00 Asia/Shanghai schedule
deploy/finance-backup.service          One-shot online backup
deploy/finance-backup.timer            Daily backup schedule
deploy/nginx-finance.conf              Port 80 reverse proxy
deploy/install.sh                      Idempotent Ubuntu installation script
tests/                                 Unit, integration, route and deployment tests
```

## Phase 1: Core Ledger and Risk Engine

### Task 1: Bootstrap the Application

**Files:**
- Create: `pyproject.toml`
- Create: `.env.example`
- Create: `finance_app/__init__.py`
- Create: `finance_app/config.py`
- Create: `finance_app/app.py`
- Create: `tests/conftest.py`
- Create: `tests/test_health.py`

- [ ] **Step 1: Write the failing health test**

```python
def test_health_returns_version(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "0.1.0"}
```

- [ ] **Step 2: Run the test and verify the package is missing**

Run: `python -m pytest tests/test_health.py -v`  
Expected: FAIL with `ModuleNotFoundError: No module named 'finance_app'`.

- [ ] **Step 3: Add package metadata, typed settings and the app factory**

Use these runtime dependencies in `pyproject.toml`: FastAPI, Uvicorn, SQLAlchemy, Alembic, Jinja2, python-multipart, pydantic-settings, argon2-cffi, itsdangerous, httpx, openpyxl, Pillow and pytesseract. Add pytest, pytest-cov, ruff and mypy to the `dev` extra. Configure pytest with `testpaths = ["tests"]`.

```python
# finance_app/config.py
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    database_url: str = "sqlite:///./data/finance.db"
    secret_key: str = "development-only-change-me"
    timezone: str = "Asia/Shanghai"
    session_https_only: bool = False
    model_config = SettingsConfigDict(env_file=".env", env_prefix="FINANCE_")

@lru_cache
def get_settings() -> Settings:
    return Settings()
```

```python
# finance_app/app.py
from fastapi import FastAPI

VERSION = "0.1.0"

def create_app() -> FastAPI:
    app = FastAPI(title="Personal Finance", version=VERSION)

    @app.get("/health", include_in_schema=False)
    def health() -> dict[str, str]:
        return {"status": "ok", "version": VERSION}

    return app

app = create_app()
```

Create a test client fixture that calls `create_app()` and uses a temporary SQLite path through `FINANCE_DATABASE_URL`.

- [ ] **Step 4: Install and verify**

Run: `python -m pip install -e ".[dev]"`  
Run: `python -m pytest tests/test_health.py -v`  
Expected: 1 test passes.

- [ ] **Step 5: Commit the bootstrap**

```bash
git add pyproject.toml .env.example finance_app tests
git commit -m "feat: bootstrap finance application"
```

### Task 2: Build the Database Foundation and Migration

**Files:**
- Create: `alembic.ini`
- Create: `alembic/env.py`
- Create: `alembic/versions/0001_initial_schema.py`
- Create: `finance_app/db.py`
- Create: `finance_app/auth/models.py`
- Create: `finance_app/ledger/models.py`
- Create: `finance_app/portfolio/models.py`
- Create: `finance_app/notifications/models.py`
- Test: `tests/test_database.py`

- [ ] **Step 1: Write failing schema and WAL tests**

```python
def test_sqlite_enables_foreign_keys_and_wal(db_session):
    assert db_session.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
    assert db_session.execute(text("PRAGMA journal_mode")).scalar_one().lower() == "wal"

def test_money_columns_store_integer_cents(db_session):
    account = Account(name="余额宝", kind="cash", currency="CNY")
    db_session.add(account)
    db_session.commit()
    assert isinstance(account.opening_balance_cents, int)
```

- [ ] **Step 2: Run the database tests**

Run: `python -m pytest tests/test_database.py -v`  
Expected: FAIL because `finance_app.db` and mapped models do not exist.

- [ ] **Step 3: Implement the schema and SQLite connection policy**

Create a SQLAlchemy `DeclarativeBase`, engine factory and request-scoped session. Apply `PRAGMA foreign_keys=ON`, `PRAGMA busy_timeout=5000`, and `PRAGMA journal_mode=WAL` on SQLite connections.

The initial migration must create these tables with UTC timestamps and integer-cent money fields:

```text
users, accounts, assets, transactions, holdings, cash_buckets, monthly_budgets,
allocation_targets, price_snapshots, portfolio_snapshots, alerts,
notification_channels, notification_deliveries, import_batches, audit_events,
app_settings, job_runs
```

Required uniqueness constraints:

```text
users.username
assets.code + assets.market
transactions.source + transactions.external_id
holdings.account_id + holdings.asset_id
monthly_budgets.month + monthly_budgets.bucket_kind
price_snapshots.asset_id + price_snapshots.valuation_date + price_snapshots.source
import_batches.sha256
app_settings.key
job_runs.job_name + job_runs.business_date
```

Model monetary fields use names ending in `_cents`; quantities and prices use `Decimal` backed by `Numeric(24, 8)`. Transactions contain `reverses_transaction_id` instead of a destructive delete flag.

- [ ] **Step 4: Apply the migration and verify tests**

Run: `python -m alembic upgrade head`  
Run: `python -m pytest tests/test_database.py -v`  
Expected: all database tests pass and `alembic current` prints `0001`.

- [ ] **Step 5: Commit the schema**

```bash
git add alembic.ini alembic finance_app/db.py finance_app/auth finance_app/ledger finance_app/portfolio finance_app/notifications tests/test_database.py
git commit -m "feat: add finance database schema"
```

### Task 3: Add Single-User Authentication and CSRF Protection

**Files:**
- Create: `finance_app/auth/service.py`
- Create: `finance_app/auth/routes.py`
- Create: `finance_app/templates/login.html`
- Modify: `finance_app/app.py`
- Test: `tests/test_auth.py`

- [ ] **Step 1: Write failing authentication tests**

```python
def test_dashboard_redirects_anonymous_user(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"

def test_login_rejects_missing_csrf(client, admin_user):
    response = client.post("/login", data={"username": "admin", "password": "secret"})
    assert response.status_code == 403

def test_login_creates_expiring_session(client, admin_user, csrf_token):
    response = client.post("/login", data={"username": "admin", "password": "secret", "csrf_token": csrf_token})
    assert response.status_code == 303
    assert "session=" in response.headers["set-cookie"]
```

- [ ] **Step 2: Verify the tests fail**

Run: `python -m pytest tests/test_auth.py -v`  
Expected: FAIL because login routes and authentication services do not exist.

- [ ] **Step 3: Implement authentication**

Use Argon2 for password hashing. Add Starlette `SessionMiddleware` with an eight-hour maximum age, `same_site="lax"`, and `https_only` from settings. Generate a CSRF token per session with `secrets.token_urlsafe(32)` and validate it with `hmac.compare_digest` on every state-changing form.

```python
def require_user(request: Request, session: Session = Depends(get_db)) -> User:
    user_id = request.session.get("user_id")
    user = session.get(User, user_id) if user_id else None
    if user is None:
        raise HTTPException(status_code=401, detail="authentication required")
    return user
```

Add CLI command `finance init-admin --username admin` that reads the password twice through `getpass`, rejects passwords shorter than 12 characters, and refuses to create a second active user.

- [ ] **Step 4: Run auth and regression tests**

Run: `python -m pytest tests/test_auth.py tests/test_health.py -v`  
Expected: all tests pass.

- [ ] **Step 5: Commit authentication**

```bash
git add finance_app/auth finance_app/templates/login.html finance_app/app.py finance_app/cli.py tests/test_auth.py
git commit -m "feat: secure single-user access"
```

### Task 4: Implement the Monthly Cash-Bucket Rules

**Files:**
- Create: `finance_app/ledger/budget.py`
- Create: `finance_app/ledger/service.py`
- Test: `tests/ledger/test_budget.py`

- [ ] **Step 1: Write failing allocation and reserve tests**

```python
def test_salary_is_allocated_in_integer_cents():
    result = allocate_salary(400_000, default_budget_rule())
    assert result == {
        BucketKind.FIXED_EXPENSE: 270_000,
        BucketKind.RESERVE: 100_000,
        BucketKind.INVESTMENT: 20_000,
        BucketKind.DISCRETIONARY: 10_000,
    }

def test_salary_must_equal_bucket_total():
    with pytest.raises(BudgetMismatch, match="allocated total"):
        allocate_salary(399_999, default_budget_rule())

def test_only_investment_bucket_is_investable():
    assert investable_cents({BucketKind.RESERVE: 100_000, BucketKind.INVESTMENT: 20_000}) == 20_000
```

- [ ] **Step 2: Verify the tests fail**

Run: `python -m pytest tests/ledger/test_budget.py -v`  
Expected: FAIL because the budget module does not exist.

- [ ] **Step 3: Implement explicit value objects and persistence**

```python
class BucketKind(StrEnum):
    FIXED_EXPENSE = "fixed_expense"
    RESERVE = "reserve"
    INVESTMENT = "investment"
    DISCRETIONARY = "discretionary"

@dataclass(frozen=True)
class BudgetRule:
    fixed_expense_cents: int
    reserve_cents: int
    investment_cents: int
    discretionary_cents: int

def default_budget_rule() -> BudgetRule:
    return BudgetRule(270_000, 100_000, 20_000, 10_000)
```

Add `post_salary_month(session, month, salary_cents, rule)` to create one monthly budget row per bucket in a transaction. Repeating the same month returns the existing rows; a different amount for an existing month raises `BudgetConflict`.

- [ ] **Step 4: Run focused and full tests**

Run: `python -m pytest tests/ledger/test_budget.py -v`  
Run: `python -m pytest -q`  
Expected: all tests pass.

- [ ] **Step 5: Commit budget rules**

```bash
git add finance_app/ledger tests/ledger/test_budget.py
git commit -m "feat: enforce monthly cash bucket rules"
```

### Task 5: Implement Idempotent Transactions, Reversals and Holdings

**Files:**
- Modify: `finance_app/ledger/service.py`
- Create: `finance_app/ledger/schemas.py`
- Test: `tests/ledger/test_transactions.py`

- [ ] **Step 1: Write failing posting tests**

```python
def test_duplicate_external_transaction_is_returned_not_reposted(session, fund_asset, fund_account):
    command = PostTransaction(source="xlsx", external_id="row-9", kind="buy", account_id=fund_account.id,
                              asset_id=fund_asset.id, amount_cents=10_000, quantity=Decimal("12.5"),
                              price=Decimal("8"), fee_cents=0, occurred_at=UTC_NOW)
    first = post_transaction(session, command)
    second = post_transaction(session, command)
    assert first.id == second.id
    assert session.scalar(select(func.count(Transaction.id))) == 1

def test_reversal_preserves_original_and_zeroes_position(session, posted_buy):
    reversal = reverse_transaction(session, posted_buy.id, "录入错误")
    assert reversal.reverses_transaction_id == posted_buy.id
    assert calculate_position(session, posted_buy.account_id, posted_buy.asset_id).quantity == Decimal("0")
```

- [ ] **Step 2: Verify failure**

Run: `python -m pytest tests/ledger/test_transactions.py -v`  
Expected: FAIL because posting and position calculation are missing.

- [ ] **Step 3: Implement ledger commands**

Validate that buys have positive quantity and amount, sells cannot exceed the current quantity, fees are non-negative, and cash transfers have no asset quantity. Use the source/external ID uniqueness constraint for idempotency. Calculate positions by summing signed transaction quantities and integer-cent cost movements; never mutate historical transaction rows. Update the `holdings` table in the same transaction as a derived cache, and test that rebuilding the cache from the ledger produces identical quantities and costs.

```python
@dataclass(frozen=True)
class Position:
    quantity: Decimal
    cost_cents: int

def signed_quantity(kind: TransactionKind, quantity: Decimal) -> Decimal:
    return -quantity if kind in {TransactionKind.SELL, TransactionKind.REVERSAL_SELL} else quantity
```

- [ ] **Step 4: Verify ledger invariants**

Run: `python -m pytest tests/ledger/test_transactions.py -v`  
Expected: duplicate import, oversell rejection, fee handling, and reversal tests pass.

- [ ] **Step 5: Commit the ledger**

```bash
git add finance_app/ledger tests/ledger/test_transactions.py
git commit -m "feat: add auditable transaction ledger"
```

### Task 6: Implement Valuation, Freshness and Risk Suggestions

**Files:**
- Create: `finance_app/portfolio/rules.py`
- Create: `finance_app/portfolio/service.py`
- Test: `tests/portfolio/test_rules.py`
- Test: `tests/portfolio/test_snapshots.py`

- [ ] **Step 1: Write failing risk tests**

```python
def test_high_risk_buy_is_paused_when_reserve_is_short():
    context = RuleContext(reserve_cents=80_000, required_reserve_cents=100_000,
                          investment_cash_cents=20_000, prices_fresh=True)
    advice = evaluate_new_investment(context, risk_level=RiskLevel.HIGH)
    assert advice.action == AdviceAction.WAIT
    assert advice.reason_code == "reserve_below_minimum"

def test_stale_price_never_produces_certain_trade_advice():
    context = RuleContext(reserve_cents=100_000, required_reserve_cents=100_000,
                          investment_cash_cents=20_000, prices_fresh=False)
    advice = evaluate_new_investment(context, risk_level=RiskLevel.MEDIUM)
    assert advice.action == AdviceAction.WAIT_FOR_DATA
    assert advice.amount_cents is None

def test_satellite_overweight_is_reduced_in_batches():
    advice = evaluate_allocation(current_bps=2500, target_bps=1000, upper_bps=1500, value_cents=100_000)
    assert advice.action == AdviceAction.REDUCE_IN_BATCHES
    assert advice.amount_cents == 10_000
```

- [ ] **Step 2: Verify failure**

Run: `python -m pytest tests/portfolio -v`  
Expected: FAIL because rule and snapshot services do not exist.

- [ ] **Step 3: Implement deterministic rules and daily snapshots**

Represent ratios in basis points. Treat a fund NAV as fresh only when its valuation date is no older than the latest expected trading day, accounting for weekends but not fabricating holiday data. Every advice record must include action, optional amount, trigger, maximum-risk text, stop-discipline text, source and source timestamp.

Use a configurable batch size in basis points; the default reduction is the lesser of 10% of the position and the amount required to return to the upper bound. Persist snapshots with `data_complete=False` if any non-cash holding lacks a fresh price.

- [ ] **Step 4: Run the rule and snapshot suite**

Run: `python -m pytest tests/portfolio -v`  
Expected: reserve, stale data, allocation boundary, integer rounding and snapshot tests pass.

- [ ] **Step 5: Commit the risk engine**

```bash
git add finance_app/portfolio tests/portfolio
git commit -m "feat: add data-aware portfolio risk engine"
```

## Phase 2: Interface and Integrations

### Task 7: Build the Responsive Dashboard Shell

**Files:**
- Create: `finance_app/web/routes.py`
- Create: `finance_app/web/viewmodels.py`
- Create: `finance_app/templates/base.html`
- Create: `finance_app/templates/dashboard.html`
- Create: `finance_app/templates/holdings.html`
- Create: `finance_app/templates/alerts.html`
- Create: `finance_app/static/app.css`
- Create: `finance_app/static/app.js`
- Modify: `finance_app/app.py`
- Test: `tests/web/test_dashboard.py`

- [ ] **Step 1: Write failing page tests**

```python
def test_dashboard_shows_cash_rule_and_data_time(authenticated_client, seeded_portfolio):
    response = authenticated_client.get("/")
    assert response.status_code == 200
    assert "流动储备金" in response.text
    assert "¥1,000.00" in response.text
    assert "数据更新时间" in response.text

def test_mobile_pages_have_viewport_and_primary_navigation(authenticated_client):
    response = authenticated_client.get("/holdings")
    assert '<meta name="viewport"' in response.text
    assert 'aria-label="主导航"' in response.text
```

- [ ] **Step 2: Verify page tests fail**

Run: `python -m pytest tests/web/test_dashboard.py -v`  
Expected: FAIL with missing routes/templates.

- [ ] **Step 3: Implement the visual system and pages**

Use a warm paper background, deep ink text, vermilion risk accents, and cyan data accents. Define CSS custom properties for color, space, radii and typography. Use `"Noto Sans SC", "Microsoft YaHei", sans-serif` for body text and `"Noto Serif SC", SimSun, serif` for display numbers. The desktop dashboard uses a 12-column grid; below 760px it becomes one column with a sticky bottom navigation.

The dashboard must render these cards from `DashboardViewModel`: total assets, reserve balance/status, investment cash, month budget completion, core/satellite allocation, fresh/stale data badge, latest advice, and seven-day snapshot series. Render an empty state instead of zero when data is unknown.

- [ ] **Step 4: Run page and accessibility smoke tests**

Run: `python -m pytest tests/web/test_dashboard.py -v`  
Expected: dashboard, holdings and alert pages pass authenticated/anonymous and empty-state cases.

- [ ] **Step 5: Run the local visual checkpoint**

Run: `python -m uvicorn finance_app.app:app --host 127.0.0.1 --port 3000`  
Open: `http://127.0.0.1:3000`  
Expected: the user confirms the desktop and mobile dashboard direction before deployment work begins. Record requested visual adjustments in this task and rerun the page tests after applying them.

- [ ] **Step 6: Commit the dashboard**

```bash
git add finance_app/web finance_app/templates finance_app/static finance_app/app.py tests/web
git commit -m "feat: add responsive finance dashboard"
```

### Task 8: Add Manual Entry and Audited Form Actions

**Files:**
- Modify: `finance_app/web/routes.py`
- Create: `finance_app/templates/transactions.html`
- Create: `finance_app/templates/transaction_form.html`
- Create: `finance_app/templates/accounts.html`
- Create: `finance_app/templates/price_form.html`
- Test: `tests/web/test_forms.py`

- [ ] **Step 1: Write failing form tests**

```python
def test_buy_form_posts_integer_cent_amount(authenticated_client, csrf_token, fund_ids):
    response = authenticated_client.post("/transactions", data={
        "csrf_token": csrf_token, "kind": "buy", "account_id": fund_ids.account,
        "asset_id": fund_ids.asset, "amount_yuan": "100.05", "quantity": "87.0000",
        "occurred_at": "2026-09-23T10:00", "note": "手工录入",
    }, follow_redirects=False)
    assert response.status_code == 303
    assert stored_transaction().amount_cents == 10_005

def test_reversal_requires_reason(authenticated_client, csrf_token, posted_buy):
    response = authenticated_client.post(f"/transactions/{posted_buy.id}/reverse",
        data={"csrf_token": csrf_token, "reason": ""})
    assert response.status_code == 422

def test_manual_price_keeps_source_and_valuation_date(authenticated_client, csrf_token, fund_asset):
    response = authenticated_client.post("/prices", data={
        "csrf_token": csrf_token, "asset_id": fund_asset.id, "price": "1.2345",
        "valuation_date": "2026-09-22", "source": "manual:alipay-screenshot",
    }, follow_redirects=False)
    assert response.status_code == 303
    assert latest_price(fund_asset.id).source == "manual:alipay-screenshot"

def test_mark_alert_read_is_audited(authenticated_client, csrf_token, alert):
    response = authenticated_client.post(f"/alerts/{alert.id}/read", data={"csrf_token": csrf_token})
    assert response.status_code == 303
    assert latest_audit_event().action == "alert.mark_read"
```

- [ ] **Step 2: Verify the forms fail**

Run: `python -m pytest tests/web/test_forms.py -v`  
Expected: FAIL because form endpoints do not exist.

- [ ] **Step 3: Implement forms and audit events**

Parse yuan strings with `Decimal`, quantize to two places, and convert to cents. Validate manual price source and valuation date, and never label it as real time. On every create/update/reversal/manual-price/mark-read action, add an `audit_events` row containing actor, action, entity type, entity ID, request ID and a JSON summary that excludes credentials. Use post/redirect/get to prevent browser resubmission.

- [ ] **Step 4: Run form tests**

Run: `python -m pytest tests/web/test_forms.py -v`  
Expected: valid transaction, validation, CSRF, reversal and audit cases pass.

- [ ] **Step 5: Commit manual entry**

```bash
git add finance_app/web finance_app/templates tests/web/test_forms.py
git commit -m "feat: add audited finance entry forms"
```

### Task 9: Add Idempotent CSV/XLSX Import and OCR Confirmation

**Files:**
- Create: `finance_app/imports/parser.py`
- Create: `finance_app/imports/ocr.py`
- Create: `finance_app/imports/routes.py`
- Create: `finance_app/templates/import_preview.html`
- Create: `tests/imports/test_parser.py`
- Create: `tests/imports/test_ocr.py`
- Create: `tests/imports/test_routes.py`

- [ ] **Step 1: Write failing import tests**

```python
def test_xlsx_normalizes_chinese_columns(sample_xlsx):
    rows = parse_portfolio_file(sample_xlsx)
    assert rows[0].asset_name == "示例基金"
    assert rows[0].market_value_cents == 12_345

def test_same_file_is_not_imported_twice(import_service, sample_xlsx):
    first = import_service.preview(sample_xlsx)
    import_service.confirm(first.id)
    second = import_service.preview(sample_xlsx)
    assert second.status == ImportStatus.ALREADY_IMPORTED

def test_ocr_result_is_candidate_only(sample_screenshot):
    preview = extract_candidates(sample_screenshot)
    assert preview.requires_confirmation is True
    assert preview.persisted_transactions == 0
```

- [ ] **Step 2: Verify importer tests fail**

Run: `python -m pytest tests/imports -v`  
Expected: FAIL because parsers and confirmation routes are missing.

- [ ] **Step 3: Implement parsing and two-step confirmation**

Accept `.csv`, `.xlsx`, `.png`, `.jpg` and `.jpeg`, enforce a 10 MiB upload limit, calculate SHA-256 before parsing, and store uploads outside the static directory. Normalize these aliases: 基金代码/代码, 基金名称/名称, 份额, 成本金额/持仓成本, 当前市值/持有金额, 可用现金, 日期.

Use `pytesseract.image_to_data(..., lang="chi_sim+eng")` and group OCR tokens by line. Extract numeric candidates with strict decimal patterns; associate labels only when they occur on the same or adjacent line. The preview page shows source text, confidence, editable normalized fields and validation errors. Only the confirmation POST writes ledger rows.

- [ ] **Step 4: Run import tests with OCR mocked at the process boundary**

Run: `python -m pytest tests/imports -v`  
Expected: file-size, path traversal, duplicate hash, column normalization, OCR candidate and confirmation tests pass.

- [ ] **Step 5: Commit import workflows**

```bash
git add finance_app/imports finance_app/templates/import_preview.html tests/imports
git commit -m "feat: add confirmed portfolio imports"
```

### Task 10: Add a Traceable Public Fund NAV Adapter

**Files:**
- Create: `finance_app/market/base.py`
- Create: `finance_app/market/eastmoney.py`
- Create: `finance_app/market/service.py`
- Test: `tests/market/test_eastmoney.py`
- Test: `tests/market/test_service.py`

- [ ] **Step 1: Write failing adapter tests using recorded JSON**

```python
def test_eastmoney_maps_daily_nav(httpx_mock):
    httpx_mock.add_response(json={"Data": {"LSJZList": [{"FSRQ": "2026-09-22", "DWJZ": "1.2345"}]}})
    quote = EastMoneyFundNavProvider().fetch("000001")
    assert quote.value == Decimal("1.2345")
    assert quote.valuation_date == date(2026, 9, 22)
    assert quote.source == "eastmoney"

def test_provider_failure_does_not_overwrite_last_good_price(httpx_mock, price_service):
    httpx_mock.add_exception(httpx.TimeoutException("timeout"))
    result = price_service.refresh("000001")
    assert result.status == RefreshStatus.FAILED
    assert result.last_good_price == Decimal("1.2000")
```

- [ ] **Step 2: Verify market tests fail**

Run: `python -m pytest tests/market -v`  
Expected: FAIL because the provider does not exist.

- [ ] **Step 3: Implement the adapter and provenance**

Request `https://api.fund.eastmoney.com/f10/lsjz` with query parameters `fundCode`, `pageIndex=1`, and `pageSize=1`, a browser user agent, and `Referer: https://fundf10.eastmoney.com/`. Use connect/read timeouts and two bounded retries. Validate the JSON shape and decimal/date formats. Persist source URL, fetched time, valuation date and error text without logging full responses.

- [ ] **Step 4: Run market and rule integration tests**

Run: `python -m pytest tests/market tests/portfolio -v`  
Expected: success, malformed JSON, timeout, stale NAV and last-good-price cases pass.

- [ ] **Step 5: Commit market data support**

```bash
git add finance_app/market tests/market
git commit -m "feat: add traceable daily fund NAV source"
```

### Task 11: Add Email and WeChat Notification Adapters

**Files:**
- Create: `finance_app/notifications/base.py`
- Create: `finance_app/notifications/email.py`
- Create: `finance_app/notifications/wechat.py`
- Create: `finance_app/notifications/service.py`
- Modify: `finance_app/config.py`
- Test: `tests/notifications/test_email.py`
- Test: `tests/notifications/test_wechat.py`
- Test: `tests/notifications/test_service.py`

- [ ] **Step 1: Write failing adapter and retry tests**

```python
def test_email_uses_tls_and_authorization_code(fake_smtp, settings):
    EmailNotifier(settings).send(Notification(title="每日理财摘要", body="无操作"))
    assert fake_smtp.starttls_called
    assert fake_smtp.login_args == (settings.smtp_username, settings.smtp_authorization_code)

@pytest.mark.parametrize("channel", ["pushplus", "serverchan", "wecom"])
def test_wechat_adapter_sends_redacted_request(channel, httpx_mock, settings):
    httpx_mock.add_response(json={"code": 0})
    build_wechat_notifier(channel, settings).send(Notification("风险提醒", "储备金不足"))
    assert settings.wechat_token not in captured_logs()

def test_delivery_stops_after_three_attempts(failing_notifier, delivery_service):
    result = delivery_service.deliver(failing_notifier, Notification("摘要", "正文"))
    assert result.attempt_count == 3
    assert result.status == DeliveryStatus.FAILED
```

- [ ] **Step 2: Verify notification tests fail**

Run: `python -m pytest tests/notifications -v`  
Expected: FAIL because notifier classes are missing.

- [ ] **Step 3: Implement adapters and bounded retries**

Read SMTP authorization codes and WeChat tokens only from environment-backed settings. Support SMTP STARTTLS/SSL, PushPlus `https://www.pushplus.plus/send`, ServerChan `https://sctapi.ftqq.com/{sendkey}.send`, and WeCom webhook POST. Normalize provider responses to `DeliveryResult`; persist attempt number, timestamp, status and a credential-free error summary.

Retry at most three times with delays of 1 and 3 seconds. Do not retry authentication errors or invalid configuration. Email remains the primary channel; a WeChat failure must not mark a successful email delivery as failed.

- [ ] **Step 4: Run notification tests**

Run: `python -m pytest tests/notifications -v`  
Expected: TLS, provider mapping, redaction, bounded retry and independent-channel tests pass.

- [ ] **Step 5: Commit notifications**

```bash
git add finance_app/notifications finance_app/config.py tests/notifications
git commit -m "feat: send audited email and WeChat alerts"
```

### Task 12: Orchestrate the Daily Check and Settings Page

**Files:**
- Modify: `finance_app/cli.py`
- Modify: `finance_app/web/routes.py`
- Create: `finance_app/templates/settings.html`
- Create: `finance_app/templates/analysis.html`
- Test: `tests/test_daily_check.py`
- Test: `tests/web/test_settings.py`

- [ ] **Step 1: Write failing orchestration tests**

```python
def test_daily_check_orders_freshness_before_advice(daily_check, spy):
    daily_check.run(date(2026, 9, 23))
    assert spy.events == ["refresh_prices", "validate_freshness", "snapshot", "evaluate_rules", "notify"]

def test_general_advice_is_combined_into_one_digest(daily_check, notifier):
    daily_check.run(date(2026, 9, 23))
    assert notifier.sent_titles == ["2026-09-23 每日理财摘要"]

def test_critical_risk_can_send_separate_alert(daily_check, notifier):
    daily_check.run(date(2026, 9, 23), reserve_cents=0)
    assert "重大风险提醒" in notifier.sent_titles
```

- [ ] **Step 2: Verify orchestration tests fail**

Run: `python -m pytest tests/test_daily_check.py tests/web/test_settings.py -v`  
Expected: FAIL because the daily command and settings page are incomplete.

- [ ] **Step 3: Implement daily-check and editable non-secret settings**

Add `finance daily-check --date YYYY-MM-DD` with a `job_runs` uniqueness guard so overlapping executions exit cleanly. Add `--scheduled`: it reads the configured Beijing time, runs once when invoked within the matching five-minute window, and otherwise exits successfully without changing state. Run refresh, freshness validation, snapshot, rules and notifications in the tested order. Include total assets, cash buckets, data time/source, actions, amount/ratio, trigger, maximum risk and stop discipline in the digest.

The settings form stores schedule, reserve target, core/satellite targets, per-satellite upper bound, stale-data threshold and enabled channel names. It never accepts or displays credential values; it displays only configured/not-configured status from environment settings.

- [ ] **Step 4: Run orchestration and full tests**

Run: `python -m pytest tests/test_daily_check.py tests/web/test_settings.py -v`  
Run: `python -m pytest -q`  
Expected: all tests pass.

- [ ] **Step 5: Commit orchestration**

```bash
git add finance_app/cli.py finance_app/web finance_app/templates/settings.html finance_app/templates/analysis.html tests
git commit -m "feat: orchestrate daily portfolio checks"
```

### Task 13: Add Exports, Online Backups and Restore Verification

**Files:**
- Create: `finance_app/ops/backup.py`
- Create: `finance_app/ops/export.py`
- Modify: `finance_app/cli.py`
- Create: `finance_app/templates/backups.html`
- Test: `tests/ops/test_backup.py`
- Test: `tests/ops/test_export.py`

- [ ] **Step 1: Write failing backup and export tests**

```python
def test_online_backup_opens_and_passes_integrity_check(populated_db, tmp_path):
    backup = create_backup(populated_db, tmp_path, keep=14)
    assert verify_backup(backup).integrity_check == "ok"

def test_backup_excludes_environment_secrets(populated_db, tmp_path, secret_token):
    backup = create_backup(populated_db, tmp_path, keep=14)
    assert secret_token.encode() not in backup.read_bytes()

def test_json_export_contains_provenance_but_no_password_hash(export_service):
    payload = export_service.to_json()
    assert "source_timestamp" in payload
    assert "password_hash" not in payload
```

- [ ] **Step 2: Verify operations tests fail**

Run: `python -m pytest tests/ops -v`  
Expected: FAIL because backup and export modules are missing.

- [ ] **Step 3: Implement safe backup, restore-check and exports**

Use `sqlite3.Connection.backup()` to create `finance-YYYYMMDD-HHMMSS.sqlite3`, run `PRAGMA integrity_check`, write a SHA-256 sidecar, and retain the newest 14 verified backups. `finance restore-check PATH` copies the candidate into a temporary directory, validates its checksum/schema/integrity, and never overwrites the live database.

Export holdings/transactions as UTF-8 BOM CSV and the full non-secret dataset as versioned JSON. Record export and backup audit events.

- [ ] **Step 4: Run operations and regression tests**

Run: `python -m pytest tests/ops -v`  
Run: `python -m pytest -q`  
Expected: backup integrity, retention, secret exclusion, restore-check and export tests pass.

- [ ] **Step 5: Commit operations support**

```bash
git add finance_app/ops finance_app/cli.py finance_app/templates/backups.html tests/ops
git commit -m "feat: add verified backups and data exports"
```

## Phase 3: Server Deployment and Acceptance

### Task 14: Create Idempotent Ubuntu Deployment Artifacts

**Files:**
- Create: `deploy/finance-app.service`
- Create: `deploy/finance-daily.service`
- Create: `deploy/finance-daily.timer`
- Create: `deploy/finance-backup.service`
- Create: `deploy/finance-backup.timer`
- Create: `deploy/nginx-finance.conf`
- Create: `deploy/logrotate-finance`
- Create: `deploy/install.sh`
- Create: `tests/deploy/test_artifacts.py`

- [ ] **Step 1: Write failing deployment contract tests**

```python
def test_application_binds_only_loopback():
    unit = Path("deploy/finance-app.service").read_text()
    assert "--host 127.0.0.1" in unit
    assert "User=financeapp" in unit

def test_nginx_does_not_serve_database_or_env_files():
    config = Path("deploy/nginx-finance.conf").read_text()
    assert "location ~ /\\." in config
    assert "deny all" in config
    assert "proxy_pass http://127.0.0.1:8000" in config

def test_daily_timer_checks_configured_time_every_five_minutes():
    timer = Path("deploy/finance-daily.timer").read_text()
    assert "OnCalendar=*-*-* *:0/5:00" in timer
    service = Path("deploy/finance-daily.service").read_text()
    assert "daily-check --scheduled" in service
```

- [ ] **Step 2: Verify deployment tests fail**

Run: `python -m pytest tests/deploy/test_artifacts.py -v`  
Expected: FAIL because deployment files do not exist.

- [ ] **Step 3: Create least-privilege, repeatable deployment files**

`install.sh` must require root, verify Ubuntu 24.04, install only Python venv/build essentials/Nginx/Tesseract Chinese language data, create the locked `financeapp` user, copy the release to `/opt/personal-finance/releases/<git-sha>`, create `/opt/personal-finance/current` symlink, create `/var/lib/personal-finance/{data,backups,uploads}`, and set ownership to `financeapp:financeapp`.

The app service uses `NoNewPrivileges=true`, `PrivateTmp=true`, `ProtectSystem=strict`, explicit writable paths, restart-on-failure, a 300 MiB memory ceiling and the environment file `/etc/personal-finance/finance.env`. Nginx listens on port 80, sets security headers, limits uploads to 10 MiB and proxies only to loopback. The daily timer wakes every five minutes; `daily-check --scheduled` consults the saved Beijing-time setting and the `job_runs` uniqueness guard, so the default is 09:00 while the settings page can change it without rewriting system files.

The installer must not change SSH settings, remove Aliyun agents, install Docker, or open the Alibaba Cloud security group. It prints the exact remaining manual security-group action instead.

- [ ] **Step 4: Run static deployment verification**

Run: `python -m pytest tests/deploy/test_artifacts.py -v`  
Run: `bash -n deploy/install.sh`  
Expected: artifact tests pass and shell syntax exits 0.

- [ ] **Step 5: Commit deployment artifacts**

```bash
git add deploy tests/deploy
git commit -m "ops: add hardened Ubuntu deployment"
```

### Task 15: Add Operating Documentation and Release Checks

**Files:**
- Create: `README.md`
- Create: `docs/operations.md`
- Create: `docs/data-import.md`
- Create: `scripts/release-check.sh`
- Create: `tests/test_security_regressions.py`

- [ ] **Step 1: Write failing release and security tests**

```python
def test_sensitive_paths_are_never_public(client):
    for path in ["/.env", "/data/finance.db", "/backups/latest.sqlite3"]:
        assert client.get(path).status_code in {404, 403}

def test_logs_redact_configured_secrets(configured_app, caplog):
    configured_app.log_configuration()
    assert "smtp_auth_code_value" not in caplog.text
    assert "wechat_token_value" not in caplog.text
```

- [ ] **Step 2: Verify the new tests fail where protection is absent**

Run: `python -m pytest tests/test_security_regressions.py -v`  
Expected: FAIL until explicit denial and log redaction are wired.

- [ ] **Step 3: Complete documentation and release checker**

Document local setup, admin initialization, data directory permissions, Excel/CSV column mapping, OCR confirmation, email configuration, all three WeChat options, daily timer changes, manual price entry, backup retention, restore-check, release rollback and log inspection.

`scripts/release-check.sh` must run Ruff, mypy, pytest with coverage, Alembic upgrade on a temporary database, import smoke tests, `bash -n` on deployment scripts, and secret-pattern scans. It exits nonzero on any failure and deletes only its own `mktemp -d` directory through a trap.

- [ ] **Step 4: Execute the complete local release gate**

Run: `bash scripts/release-check.sh`  
Expected: lint and type checks pass; all tests pass; coverage is at least 85%; migration, shell syntax and secret scans pass.

- [ ] **Step 5: Commit release documentation**

```bash
git add README.md docs scripts tests/test_security_regressions.py finance_app
git commit -m "docs: add finance platform operations guide"
```

### Task 16: Deploy to Alibaba Cloud and Perform Acceptance Tests

**Files:**
- Modify on server: `/etc/personal-finance/finance.env`
- Create on server: `/opt/personal-finance/releases/<git-sha>`
- Create on server: `/var/lib/personal-finance/`
- Create on server: `/etc/systemd/system/finance-*.service`
- Create on server: `/etc/systemd/system/finance-*.timer`
- Create on server: `/etc/nginx/sites-available/personal-finance`
- Record locally: `docs/deployment-verification-2026-09-23.md`

- [ ] **Step 1: Capture a read-only pre-deployment baseline**

Run over SSH: `uname -a; free -h; df -h /; systemctl --failed; ss -lntp; systemctl is-active ssh aliyun hbrclient hbrclientupdater`  
Expected: Ubuntu 24.04, adequate disk, no failed units, SSH active, Aliyun/backup agents active, and no public application port yet.

- [ ] **Step 2: Generate secrets locally and deploy the release**

Generate a 32-byte session secret and a separate initial administrator password without printing either value to logs. Copy the committed release, run `deploy/install.sh`, write `/etc/personal-finance/finance.env` with mode `600`, run migrations, and create the administrator interactively. Do not place SMTP or WeChat secrets into shell history.

- [ ] **Step 3: Verify locally on the server before opening port 80**

Run over SSH:

```bash
sudo -u financeapp /opt/personal-finance/current/.venv/bin/finance daily-check --dry-run
curl --fail --silent http://127.0.0.1/health
systemctl is-active finance-app nginx
systemctl is-enabled finance-daily.timer finance-backup.timer
ss -lntp
```

Expected: dry run succeeds without sending messages; health returns status/version; services and timers are active; Uvicorn is bound only to `127.0.0.1:8000`; Nginx listens on port 80.

- [ ] **Step 4: Ask the user to open Alibaba Cloud TCP 80 and verify externally**

After the user confirms the security-group change, run locally: `curl.exe --fail --silent http://121.41.225.151/health`  
Expected: JSON health response. Verify anonymous dashboard redirects to login, then ask the user to sign in and change the generated password.

- [ ] **Step 5: Configure and test notification channels without exposing credentials**

Ask the user for the SMTP provider/recipient and one WeChat provider. Guide the user to enter authorization codes/tokens directly into the protected server environment file or an interactive no-echo prompt. Send one message titled `个人理财平台测试通知`; verify one delivery record per enabled channel and no secret values in journald.

- [ ] **Step 6: Import current portfolio with explicit confirmation**

Upload the existing workbook, preview normalized rows, and require the user to confirm totals before committing. Record unknown prices as missing, record the sold satellite communications fund and partial gold sale from verified transaction data only, and keep unverified screenshot values as candidates.

- [ ] **Step 7: Exercise backup and rollback paths**

Run an online backup, execute `finance restore-check` on it, restart the application, and verify the same dashboard totals. Confirm the previous release directory remains available and document the symlink rollback command without executing it.

- [ ] **Step 8: Record final acceptance evidence**

Create `docs/deployment-verification-2026-09-23.md` containing commit SHA, service/timer states, listening ports, health result, test count, backup integrity result, notification delivery IDs with tokens redacted, imported row counts, and unresolved missing-data items.

- [ ] **Step 9: Commit acceptance evidence**

```bash
git add docs/deployment-verification-2026-09-23.md
git commit -m "ops: record production deployment verification"
```

## Final Acceptance Checklist

- [ ] `4000.00` yuan is split exactly into `2700.00 / 1000.00 / 200.00 / 100.00`.
- [ ] A reserve balance below `1000.00` blocks new high-risk buy advice.
- [ ] A stale or missing price produces `等待数据`, never fabricated real-time advice.
- [ ] Duplicate spreadsheet imports create no duplicate transactions or cash movements.
- [ ] Dashboard, holdings, alerts and entry forms work on desktop and mobile widths.
- [ ] Anonymous users cannot reach finance data; CSRF protects every state change.
- [ ] Email and the selected WeChat provider show traceable success/failure records.
- [ ] The app and SQLite are not directly exposed; only SSH 22 and Nginx 80 are public.
- [ ] A verified backup restores into a temporary database with matching totals.
- [ ] SSH, `aliyun`, `hbrclient`, and `hbrclientupdater` remain active after deployment.
- [ ] No Docker, 1Panel, OpenClaw or automatic trading component is installed.
