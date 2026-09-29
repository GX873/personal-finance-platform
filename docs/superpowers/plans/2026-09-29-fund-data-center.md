# Fund Data Center Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a fund-only data center that separates official NAV from intraday estimates, adds historical risk analytics and holding tracking, preserves editable manual NAV records, and refreshes estimates only during A-share trading sessions.

**Architecture:** External data remains behind narrow providers in `finance_app.market`; background jobs validate and persist quotes in the existing `price_snapshots` table. A dedicated read-only view-model module builds fund pages from local database rows, while FastAPI routes only coordinate authentication, forms, refresh actions, and rendering. Official NAV remains the only input to portfolio value and risk calculations.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy, Jinja2, httpx, efinance 0.5.9, the existing xalpha-compatible adapter boundary, pytest, systemd.

---

## File Map

**Create:**

- `finance_app/market/tiantian_estimate.py` - parse and fetch Tiantian Fund JSONP estimates.
- `finance_app/market/analytics.py` - period selection, annualized metrics, and risk labels.
- `finance_app/market/intraday_job.py` - trading-session gate and held-fund refresh orchestration.
- `finance_app/web/fund_center.py` - database-only fund data center view models.
- `tests/market/test_tiantian_estimate.py` - provider contract and malformed-response tests.
- `tests/market/test_analytics.py` - period and metric tests.
- `tests/market/test_intraday_job.py` - session, holiday, held-fund, and failure-isolation tests.
- `tests/web/test_fund_center.py` - view model, pages, refresh cooldown, and delete tests.
- `deploy/finance-market.service` - locked-down intraday refresh oneshot.
- `deploy/finance-market.timer` - five-minute weekday trigger.

**Modify:**

- `finance_app/market/base.py` - name the intraday provider protocol explicitly.
- `finance_app/market/service.py` - make fallback refresh quote-type aware.
- `finance_app/market/xalpha_adapter.py` - return missing metrics for insufficient data and annualize volatility by 252 trading days.
- `finance_app/cli.py` - add the `market-refresh --scheduled` command.
- `finance_app/web/routes.py` - add fund center, manual refresh, and manual NAV delete routes.
- `finance_app/templates/base.html` - rename the navigation entry to 基金数据.
- `finance_app/templates/price_form.html` - render the four fund tabs and retain manual editing.
- `finance_app/static/forms.css` - add the responsive fund center layout and stable chart dimensions.
- `deploy/install.sh` - install and enable the new systemd units.
- `tests/market/test_service.py` - protect quote-type isolation and estimate fallback behavior.
- `tests/market/test_adapters.py` - lock the revised analytics boundary.
- `tests/test_daily_check.py` - preserve the existing daily command while adding the new CLI command.
- `tests/web/test_forms.py` - preserve manual NAV regression coverage.
- `tests/deploy/test_artifacts.py` - verify the new units and installer.
- `README.md` - document data sources, quote semantics, schedules, and operations.

No database migration is planned: `price_snapshots.quote_type`, provenance fields, and per-day/per-source identity already support the required storage. Estimate providers must use source identifiers distinct from official providers, such as `tiantian:estimate` and `efinance:estimate`.

### Task 1: Tiantian intraday estimate provider

**Files:**

- Create: `finance_app/market/tiantian_estimate.py`
- Modify: `finance_app/market/base.py`
- Create: `tests/market/test_tiantian_estimate.py`

- [ ] **Step 1: Write failing provider tests**

```python
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest

from finance_app.market.base import MarketDataError, QuoteType
from finance_app.market.tiantian_estimate import TiantianEstimateProvider

NOW = datetime(2026, 9, 29, 6, 0, tzinfo=UTC)


def response(body: str, status: int = 200) -> httpx.Response:
    return httpx.Response(status, text=body, request=httpx.Request("GET", "https://fundgz.1234567.com.cn/js/000001.js"))


class Client:
    def __init__(self, result: httpx.Response):
        self.result = result

    def get(self, url: str, **kwargs) -> httpx.Response:
        assert url == "https://fundgz.1234567.com.cn/js/000001.js"
        return self.result


def test_maps_jsonp_to_intraday_quote():
    body = 'jsonpgz({"fundcode":"000001","name":"测试基金","jzrq":"2026-09-28","dwjz":"1.20","gsz":"1.2345","gszzl":"2.88","gztime":"2026-09-29 14:05"});'
    quote = TiantianEstimateProvider(Client(response(body)), clock=lambda: NOW).fetch("000001")
    assert quote.value == Decimal("1.2345")
    assert quote.valuation_date == date(2026, 9, 29)
    assert quote.quote_type is QuoteType.INTRADAY_ESTIMATE
    assert quote.source == "tiantian:estimate"


@pytest.mark.parametrize("body", ["", "jsonpgz();", "jsonpgz({bad});", 'jsonpgz({"fundcode":"999999","gsz":"1.2","gztime":"2026-09-29 14:05"});', 'jsonpgz({"fundcode":"000001","gsz":"0","gztime":"2026-09-29 14:05"});'])
def test_rejects_unusable_jsonp(body: str):
    with pytest.raises(MarketDataError):
        TiantianEstimateProvider(Client(response(body)), clock=lambda: NOW).fetch("000001")
```

- [ ] **Step 2: Run the tests and verify the missing module failure**

Run: `python -m pytest tests/market/test_tiantian_estimate.py -q`

Expected: FAIL because `finance_app.market.tiantian_estimate` does not exist.

- [ ] **Step 3: Add the protocol alias and provider**

Add to `finance_app/market/base.py`:

```python
class IntradayEstimateProvider(FundNavProvider, Protocol):
    """Provider whose fetch result must be an intraday estimate."""
```

Create `finance_app/market/tiantian_estimate.py` with a strict JSONP parser, an injected `httpx.Client`, Shanghai time parsing, HTTPS-only provenance, finite positive decimal validation through `FundNavQuote`, three bounded attempts, and sanitized `MarketDataError` values. The provider's public contract is:

```python
class TiantianEstimateProvider:
    source = "tiantian:estimate"
    source_url = "https://fundgz.1234567.com.cn/"

    def __init__(self, client: httpx.Client | None = None, *, clock: Callable[[], datetime] = utc_now) -> None:
        self._owned_client = client is None
        self._client = client or httpx.Client(timeout=5.0, follow_redirects=False)
        self._clock = clock

    def fetch(self, fund_code: str) -> FundNavQuote:
        if not re.fullmatch(r"[0-9]{6}", fund_code):
            raise ValueError("fund code must contain six digits")
        url = f"https://fundgz.1234567.com.cn/js/{fund_code}.js"
        payload = self._request_and_parse(url)
        if payload.get("fundcode") != fund_code:
            raise self._error("fund_code_mismatch", "estimate fund code did not match", url)
        observed_at = datetime.strptime(payload["gztime"], "%Y-%m-%d %H:%M").replace(tzinfo=SHANGHAI)
        return FundNavQuote(
            value=Decimal(payload["gsz"]),
            valuation_date=observed_at.date(),
            source=self.source,
            source_url=url,
            fetched_at=self._clock(),
            quote_type=QuoteType.INTRADAY_ESTIMATE,
        )

    def close(self) -> None:
        if self._owned_client:
            self._client.close()
```

The private parser must accept only `jsonpgz(<JSON object>);`, cap response size at 64 KiB, call `response.raise_for_status()`, and map transport, HTTP, parse, missing-field, timestamp, and NAV errors to short error codes without embedding the upstream body.

- [ ] **Step 4: Run provider tests**

Run: `python -m pytest tests/market/test_tiantian_estimate.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the provider**

```bash
git add finance_app/market/base.py finance_app/market/tiantian_estimate.py tests/market/test_tiantian_estimate.py
git commit -m "feat: add fund intraday estimate provider"
```

### Task 2: Quote-type-aware fallback refresh

**Files:**

- Modify: `finance_app/market/service.py`
- Modify: `tests/market/test_service.py`

- [ ] **Step 1: Write failing service tests**

Add tests proving that an estimate chain rejects official NAV, falls back to the second estimate provider, returns the last estimate rather than an official NAV on failure, and never calls `refresh_current_snapshot_after_price_update` for estimates:

```python
def test_estimate_fallback_requires_intraday_quote_type(session, monkeypatch):
    primary = StubProvider(quote=official_quote())
    fallback = StubProvider(quote=estimate_quote(source="efinance:estimate"))
    recalculated = False

    def mark_recalculated(*args, **kwargs):
        nonlocal recalculated
        recalculated = True

    monkeypatch.setattr("finance_app.market.service.refresh_current_snapshot_after_price_update", mark_recalculated)
    result = FundPriceService(session, primary, clock=lambda: NOW).refresh_with_fallback(
        "000001", [primary, fallback], required_quote_type=QuoteType.INTRADAY_ESTIMATE
    )
    assert result.status is RefreshStatus.SUCCESS
    assert result.snapshot.quote_type == "intraday_estimate"
    assert result.snapshot.source == "efinance:estimate"
    assert recalculated is False


def test_failed_estimate_refresh_returns_last_estimate_not_official(session):
    add_snapshot(session, source="eastmoney", quote_type="official_nav", price="1.20")
    estimate = add_snapshot(session, source="tiantian:estimate", quote_type="intraday_estimate", price="1.21")
    result = FundPriceService(session, FailingProvider(), clock=lambda: NOW).refresh(
        "000001", required_quote_type=QuoteType.INTRADAY_ESTIMATE
    )
    assert result.last_good_snapshot is estimate
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `python -m pytest tests/market/test_service.py -k "estimate_fallback or failed_estimate" -q`

Expected: FAIL because fallback is hard-coded to official NAV and `_last_good` is official-only.

- [ ] **Step 3: Generalize the service without changing official callers**

Change the signatures and lookups to:

```python
def refresh_with_fallback(
    self,
    fund_code: str,
    providers: list[FundNavProvider],
    *,
    required_quote_type: QuoteType = QuoteType.OFFICIAL_NAV,
) -> RefreshResult:
    if not providers:
        raise ValueError("at least one NAV provider is required")
    attempts: list[ProviderAttempt] = []
    last_result: RefreshResult | None = None
    for provider in providers:
        result = FundPriceService(self._session, provider, clock=self._clock).refresh(
            fund_code, required_quote_type=required_quote_type
        )
        matches = (
            result.snapshot is not None
            and QuoteType(result.snapshot.quote_type) is required_quote_type
        )
        attempt_status = result.status if matches else RefreshStatus.FAILED
        attempt_error = result.error_summary
        if result.snapshot is not None and not matches:
            attempt_error = f"provider returned {result.snapshot.quote_type}; required {required_quote_type.value}"
        attempts.append(ProviderAttempt(provider.source, attempt_status, attempt_error))
        last_result = result
        if result.status is RefreshStatus.SUCCESS and matches:
            return replace(result, provider_attempts=tuple(attempts))
    assert last_result is not None
    asset = self._session.scalar(
        select(Asset).where(
            Asset.code == fund_code,
            Asset.market == "CN",
            Asset.asset_class == "fund",
        )
    )
    last_good = (
        self._last_good(asset.id, required_quote_type) if asset is not None else None
    )
    return replace(
        last_result,
        status=RefreshStatus.FAILED,
        is_fresh=False,
        snapshot=None,
        last_good_snapshot=last_good,
        last_good_price=last_good.price if last_good is not None else None,
        error_summary=f"no usable {required_quote_type.value} from configured providers",
        provider_attempts=tuple(attempts),
    )


def _last_good(
    self,
    asset_id: int,
    quote_type: QuoteType = QuoteType.OFFICIAL_NAV,
) -> PriceSnapshot | None:
    rows = self._session.scalars(
        select(PriceSnapshot).where(
            PriceSnapshot.asset_id == asset_id,
            PriceSnapshot.error_text.is_(None),
            PriceSnapshot.price > 0,
            PriceSnapshot.quote_type == quote_type.value,
        )
    )
    return max(rows, key=price_selection_key, default=None)
```

Pass `required_quote_type` into every failure lookup and preserve the current default so all official NAV callers retain existing behavior. Keep portfolio snapshot recalculation inside the existing `QuoteType.OFFICIAL_NAV` branch.

- [ ] **Step 4: Run market service regression tests**

Run: `python -m pytest tests/market/test_service.py -q`

Expected: PASS.

- [ ] **Step 5: Commit quote isolation**

```bash
git add finance_app/market/service.py tests/market/test_service.py
git commit -m "feat: isolate intraday estimate fallback"
```

### Task 3: Period analytics and risk classification

**Files:**

- Create: `finance_app/market/analytics.py`
- Modify: `finance_app/market/xalpha_adapter.py`
- Create: `tests/market/test_analytics.py`
- Modify: `tests/market/test_adapters.py`

- [ ] **Step 1: Write failing analytics tests**

```python
from datetime import date
from decimal import Decimal

from finance_app.market.analytics import FundMetrics, Period, calculate_metrics, period_start
from finance_app.market.xalpha_adapter import XalphaAdapter


def test_period_start_uses_calendar_months_and_all_has_no_cutoff():
    end = date(2026, 9, 29)
    assert period_start(Period.ONE_MONTH, end) == date(2026, 8, 29)
    assert period_start(Period.THREE_MONTHS, end) == date(2026, 6, 29)
    assert period_start(Period.SIX_MONTHS, end) == date(2026, 3, 29)
    assert period_start(Period.ONE_YEAR, end) == date(2025, 9, 29)
    assert period_start(Period.ALL, end) is None


def test_metrics_are_annualized_and_classified():
    metrics = calculate_metrics([Decimal("1.00"), Decimal("1.02"), Decimal("0.99"), Decimal("1.05")])
    assert isinstance(metrics, FundMetrics)
    assert metrics.total_return == Decimal("0.05")
    assert metrics.max_drawdown < 0
    assert metrics.annualized_volatility > 0
    assert metrics.risk_label in {"较低", "中等", "较高"}


def test_insufficient_history_is_missing_not_zero():
    adapter = XalphaAdapter([Decimal("1.00")])
    assert adapter.total_return() is None
    assert adapter.max_drawdown() is None
    assert adapter.volatility() is None
```

- [ ] **Step 2: Run analytics tests and verify failures**

Run: `python -m pytest tests/market/test_analytics.py tests/market/test_adapters.py -q`

Expected: FAIL because period analytics do not exist and insufficient values currently become zero.

- [ ] **Step 3: Implement deterministic analytics**

Create these public types and functions:

```python
class Period(StrEnum):
    ONE_MONTH = "1m"
    THREE_MONTHS = "3m"
    SIX_MONTHS = "6m"
    ONE_YEAR = "1y"
    ALL = "all"


@dataclass(frozen=True)
class FundMetrics:
    total_return: Decimal | None
    max_drawdown: Decimal | None
    annualized_volatility: Decimal | None
    risk_label: str | None


def calculate_metrics(values: Sequence[Decimal]) -> FundMetrics:
    adapter = XalphaAdapter(values)
    total_return = adapter.total_return()
    max_drawdown = adapter.max_drawdown()
    volatility = adapter.volatility()
    if volatility is None or max_drawdown is None:
        label = None
    elif volatility <= Decimal("0.10") and max_drawdown >= Decimal("-0.10"):
        label = "较低"
    elif volatility <= Decimal("0.20") and max_drawdown >= Decimal("-0.20"):
        label = "中等"
    else:
        label = "较高"
    return FundMetrics(total_return, max_drawdown, volatility, label)
```

Update `XalphaAdapter.volatility()` to multiply sample daily volatility by `Decimal(252).sqrt()`. Return `None` from all metrics when fewer than two NAV observations exist, and from volatility when fewer than three observations exist. Keep all arithmetic in `Decimal` and keep the optional `xalpha` dependency behind this adapter instead of allowing library objects into the web layer.

- [ ] **Step 4: Run analytics tests**

Run: `python -m pytest tests/market/test_analytics.py tests/market/test_adapters.py -q`

Expected: PASS.

- [ ] **Step 5: Commit analytics**

```bash
git add finance_app/market/analytics.py finance_app/market/xalpha_adapter.py tests/market/test_analytics.py tests/market/test_adapters.py
git commit -m "feat: add fund period risk analytics"
```

### Task 4: Database-only fund center view models

**Files:**

- Create: `finance_app/web/fund_center.py`
- Create: `tests/web/test_fund_center.py`

- [ ] **Step 1: Write failing view-model tests**

Seed two held funds, one cleared fund, official history, and one intraday estimate. Assert:

```python
def test_fund_center_uses_official_nav_for_value_and_estimate_only_for_preview(db_session):
    seeded = seed_fund_center(db_session)
    vm = build_fund_center(db_session, selected_asset_id=seeded.asset_id, period="1y", now=NOW)
    assert [row["asset_id"] for row in vm["funds"]] == seeded.held_asset_ids
    assert vm["selected"]["official_nav"] == "1.20000000"
    assert vm["selected"]["intraday_estimate"] == "1.23000000"
    assert vm["selected"]["official_value_cents"] == 1200
    assert vm["selected"]["estimated_value_cents"] == 1230
    assert vm["selected"]["data_source"] == "eastmoney"
    assert vm["metrics"]["total_return"] is not None


def test_fund_center_never_substitutes_missing_values_with_zero(db_session):
    asset = seed_holding_without_prices(db_session)
    vm = build_fund_center(db_session, selected_asset_id=asset.id, period="1y", now=NOW)
    assert vm["selected"]["official_nav"] is None
    assert vm["selected"]["official_value_cents"] is None
    assert vm["metrics"]["total_return"] is None


def test_history_and_metrics_ignore_intraday_rows(db_session):
    asset = seed_history_with_large_intraday_outlier(db_session)
    vm = build_fund_center(db_session, selected_asset_id=asset.id, period="1m", now=NOW)
    assert all(point["quote_type"] == "official_nav" for point in vm["history"])
    assert vm["metrics"]["total_return"] == Decimal("0.10")
```

- [ ] **Step 2: Run the view-model tests and verify module failure**

Run: `python -m pytest tests/web/test_fund_center.py -k "fund_center or history" -q`

Expected: FAIL because `finance_app.web.fund_center` does not exist.

- [ ] **Step 3: Implement a focused read model**

Create:

```python
def build_fund_center(
    db: Session,
    *,
    selected_asset_id: int | None,
    period: str,
    now: datetime,
) -> dict[str, object]:
    selected_period = Period(period) if period in {item.value for item in Period} else Period.ONE_YEAR
    holdings = _aggregate_held_funds(db)
    selected = _select_fund(holdings, selected_asset_id)
    if selected is None:
        return empty_fund_center(selected_period)
    official = _latest_quote(db, selected.asset_id, QuoteType.OFFICIAL_NAV, now)
    estimate = _latest_quote(db, selected.asset_id, QuoteType.INTRADAY_ESTIMATE, now)
    history = _official_history(db, selected.asset_id, selected_period, now)
    metrics = calculate_metrics([row.price for row in history]) if history else FundMetrics(None, None, None, None)
    return _serialize_center(holdings, selected, official, estimate, history, metrics, selected_period)
```

Use SQLAlchemy queries that:

- aggregate positive `Holding.quantity` and `cost_cents` by fund asset;
- require `Asset.asset_class == "fund"` and `Asset.market == "CN"`;
- filter every market row by positive price, no error, `fetched_at <= now`, and the requested `quote_type`;
- calculate official and estimated values separately with `value_cents`;
- calculate holding return only from official value and cost;
- produce fixed-viewBox SVG chart coordinates server-side so the template contains no chart library;
- sort fund tracking rows by known official holding value descending, then fund code;
- preserve `None` for every unavailable value.

- [ ] **Step 4: Run view-model tests**

Run: `python -m pytest tests/web/test_fund_center.py -k "fund_center or history" -q`

Expected: PASS.

- [ ] **Step 5: Commit the read model**

```bash
git add finance_app/web/fund_center.py tests/web/test_fund_center.py
git commit -m "feat: build fund data center read model"
```

### Task 5: Fund pages, manual refresh, and manual NAV deletion

**Files:**

- Modify: `finance_app/web/routes.py`
- Modify: `tests/web/test_fund_center.py`
- Modify: `tests/web/test_forms.py`

- [ ] **Step 1: Write failing route tests**

Add tests for authenticated page access, valid tabs and periods, refresh quote isolation, 60-second cooldown, manual-only deletion, CSRF, audit events, and portfolio recalculation:

```python
def test_fund_center_defaults_to_quotes_and_one_year(client, db_session):
    seed_fund_center(db_session)
    login(client)
    response = client.get("/funds")
    assert response.status_code == 200
    assert 'data-active-tab="quotes"' in response.text
    assert 'aria-current="page"' in response.text


def test_manual_intraday_refresh_is_csrf_protected_and_cooled_down(client, db_session, monkeypatch):
    asset = seed_held_fund(db_session)
    install_stub_estimate_chain(monkeypatch, value="1.23")
    login(client)
    token = csrf(client, f"/funds?asset_id={asset.id}")
    first = client.post(f"/funds/{asset.id}/refresh", data={"csrf_token": token})
    second = client.post(f"/funds/{asset.id}/refresh", data={"csrf_token": token})
    assert first.status_code == 303
    assert second.status_code == 303
    assert count_provider_calls() == 1
    assert db_session.scalar(select(PriceSnapshot).where(PriceSnapshot.quote_type == "intraday_estimate")) is not None


def test_only_manual_nav_can_be_deleted(client, db_session):
    manual, automatic = seed_manual_and_automatic_prices(db_session)
    login(client)
    token = csrf(client, "/prices/new")
    assert client.post(f"/prices/{automatic.id}/delete", data={"csrf_token": token}).status_code == 403
    assert client.post(f"/prices/{manual.id}/delete", data={"csrf_token": token}).status_code == 303
    assert db_session.get(PriceSnapshot, manual.id) is None
```

- [ ] **Step 2: Run the route tests and verify failures**

Run: `python -m pytest tests/web/test_fund_center.py tests/web/test_forms.py -k "fund or manual_price" -q`

Expected: FAIL for missing routes and delete behavior.

- [ ] **Step 3: Add a dedicated page helper and routes**

Add a `_fund_page()` helper that builds the existing form context plus `build_fund_center()`. Add:

```python
@router.get("/funds", response_class=HTMLResponse)
def fund_center_page(request: Request, db: Annotated[Session, Depends(get_db)], tab: str = "quotes", asset_id: int | None = None, period: str = "1y"):
    user = current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    return _fund_page(request, db, user, tab=tab, asset_id=asset_id, period=period)


@router.post("/funds/{asset_id}/refresh", dependencies=[Depends(require_csrf)])
def refresh_fund_estimate(asset_id: int, request: Request, db: Annotated[Session, Depends(get_db)], user: Annotated[User, Depends(require_user)]):
    asset = require_held_fund(db, asset_id)
    if not manual_refresh_allowed(db, asset.id, utc_now(), cooldown_seconds=60):
        return RedirectResponse(f"/funds?asset_id={asset.id}&refresh=cooldown", status_code=303)
    primary = TiantianEstimateProvider(clock=utc_now)
    try:
        result = FundPriceService(db, primary, clock=utc_now).refresh_with_fallback(
            asset.code,
            [primary, EfinanceEstimateAdapter(clock=utc_now)],
            required_quote_type=QuoteType.INTRADAY_ESTIMATE,
        )
        record_manual_refresh_audit(db, user, asset.id, result)
        db.commit()
    finally:
        primary.close()
    state = "success" if result.status is RefreshStatus.SUCCESS else "failed"
    return RedirectResponse(f"/funds?asset_id={asset.id}&refresh={state}", status_code=303)


@router.post("/prices/{price_id}/delete", dependencies=[Depends(require_csrf)])
def delete_price(price_id: int, request: Request, db: Annotated[Session, Depends(get_db)], user: Annotated[User, Depends(require_user)]):
    snapshot = db.get(PriceSnapshot, price_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Price not found")
    if not snapshot.source.startswith("manual:"):
        raise HTTPException(status_code=403, detail="Only manual prices can be deleted")
    deleted_id = snapshot.id
    db.delete(snapshot)
    db.flush()
    refresh_current_snapshot_after_price_update(db, now=utc_now())
    db.add(audit_event(user=user, event_type="price.deleted", action="price.delete", entity_type="price_snapshot", entity_id=deleted_id, summary={"asset_id": snapshot.asset_id, "valuation_date": snapshot.valuation_date.isoformat(), "source": snapshot.source}))
    db.commit()
    return RedirectResponse("/prices/new", status_code=303)
```

Validate `tab` against `quotes`, `risk`, `holdings`, and `manual`; invalid values fall back to `quotes`. Record every manual refresh attempt as a user audit event so failed requests also activate the cooldown. Do not catch database or portfolio recalculation failures as market-provider failures.

- [ ] **Step 4: Run route and form tests**

Run: `python -m pytest tests/web/test_fund_center.py tests/web/test_forms.py -q`

Expected: PASS.

- [ ] **Step 5: Commit route behavior**

```bash
git add finance_app/web/routes.py tests/web/test_fund_center.py tests/web/test_forms.py
git commit -m "feat: add fund center routes and manual nav deletion"
```

### Task 6: Four-tab fund interface and responsive chart

**Files:**

- Modify: `finance_app/templates/base.html`
- Modify: `finance_app/templates/price_form.html`
- Modify: `finance_app/static/forms.css`
- Modify: `tests/web/test_fund_center.py`
- Modify: `tests/web/test_forms.py`

- [ ] **Step 1: Write failing rendered-page assertions**

```python
def test_fund_center_renders_only_fund_information(client, db_session):
    asset = seed_fund_center(db_session).selected_asset
    login(client)
    response = client.get(f"/funds?asset_id={asset.id}&period=1y")
    assert "行情" in response.text
    assert "风险分析" in response.text
    assert "持仓跟踪" in response.text
    assert "手工净值" in response.text
    assert "正式净值" in response.text
    assert "盘中估值" in response.text
    assert "数据来源" in response.text
    assert "重仓股" not in response.text
    assert "确认陈旧" not in response.text


def test_manual_tab_has_edit_and_delete_controls(client, db_session):
    row = seed_manual_price(db_session)
    login(client)
    response = client.get("/prices/new")
    assert f'/prices/{row.id}/edit' in response.text
    assert f'action="/prices/{row.id}/delete"' in response.text
```

- [ ] **Step 2: Run rendered-page tests and verify failure**

Run: `python -m pytest tests/web/test_fund_center.py tests/web/test_forms.py -k "renders or manual_tab" -q`

Expected: FAIL because the four-tab interface is not rendered.

- [ ] **Step 3: Build the server-rendered interface**

Update the navigation label to `基金数据` and link to `/funds`. Rebuild `price_form.html` with:

```html
<nav class="fund-tabs" aria-label="基金数据视图" data-active-tab="{{ active_tab }}">
  <a href="/funds?tab=quotes&asset_id={{ vm.selected.asset_id or '' }}&period={{ vm.period }}" {{ 'aria-current=page' if active_tab == 'quotes' }}>行情</a>
  <a href="/funds?tab=risk&asset_id={{ vm.selected.asset_id or '' }}&period={{ vm.period }}" {{ 'aria-current=page' if active_tab == 'risk' }}>风险分析</a>
  <a href="/funds?tab=holdings&asset_id={{ vm.selected.asset_id or '' }}&period={{ vm.period }}" {{ 'aria-current=page' if active_tab == 'holdings' }}>持仓跟踪</a>
  <a href="/prices/new" {{ 'aria-current=page' if active_tab == 'manual' }}>手工净值</a>
</nav>
```

Use one conditional section per tab. The quote tab contains a held-fund selector, official and estimate summary, source/time metadata, period links, a fixed `viewBox="0 0 600 220"` SVG history chart, and a CSRF-protected refresh form. The risk tab shows three compact metrics and the risk label. The holdings tab is a responsive table containing only fund-level values. The manual tab keeps the current edit form, adds the selected fund quick summary, and adds a delete form with `onsubmit="return confirm('确认删除这条手工净值？')"` for each manual row.

Add CSS with stable dimensions:

```css
.fund-center-grid { display: grid; grid-template-columns: minmax(12rem, 18rem) minmax(0, 1fr); gap: 1rem; }
.fund-chart { width: 100%; aspect-ratio: 30 / 11; min-height: 13rem; }
.fund-metrics { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: .75rem; }
.quote-estimate { color: var(--warning-text); }
@media (max-width: 760px) {
  .fund-center-grid, .fund-metrics { grid-template-columns: 1fr; }
  .fund-tabs { overflow-x: auto; }
}
```

Do not add stock labels, promotional copy, a client-side chart framework, or automatic browser polling.

- [ ] **Step 4: Run web tests**

Run: `python -m pytest tests/web/test_fund_center.py tests/web/test_forms.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the interface**

```bash
git add finance_app/templates/base.html finance_app/templates/price_form.html finance_app/static/forms.css tests/web/test_fund_center.py tests/web/test_forms.py
git commit -m "feat: add responsive fund data center interface"
```

### Task 7: Trading-session refresh job and CLI

**Files:**

- Create: `finance_app/market/intraday_job.py`
- Modify: `finance_app/cli.py`
- Create: `tests/market/test_intraday_job.py`

- [ ] **Step 1: Write failing session and orchestration tests**

```python
@pytest.mark.parametrize("local_time", ["2026-09-29T09:30:00+08:00", "2026-09-29T11:30:00+08:00", "2026-09-29T13:00:00+08:00", "2026-09-29T15:00:00+08:00"])
def test_trading_window_includes_session_boundaries(local_time):
    assert is_intraday_refresh_time(datetime.fromisoformat(local_time), frozenset())


@pytest.mark.parametrize("local_time", ["2026-09-29T09:29:00+08:00", "2026-09-29T11:31:00+08:00", "2026-09-29T12:30:00+08:00", "2026-09-29T15:01:00+08:00", "2026-10-03T10:00:00+08:00"])
def test_trading_window_excludes_closed_times(local_time):
    assert not is_intraday_refresh_time(datetime.fromisoformat(local_time), frozenset())


def test_configured_holiday_is_skipped():
    now = datetime.fromisoformat("2026-10-01T14:00:00+08:00")
    assert not is_intraday_refresh_time(now, frozenset({date(2026, 10, 1)}))


def test_job_refreshes_each_held_fund_and_isolates_failures(db_session):
    held = seed_two_held_funds_and_one_cleared(db_session)
    provider = PerCodeProvider(failing_code=held.second_code)
    result = IntradayRefreshJob(db_session, providers=[provider], clock=lambda: TRADING_NOW).run_scheduled()
    assert result.attempted == 2
    assert result.succeeded == 1
    assert result.failed == 1
    assert held.cleared_code not in provider.calls
```

- [ ] **Step 2: Run tests and verify the missing module failure**

Run: `python -m pytest tests/market/test_intraday_job.py -q`

Expected: FAIL because the intraday job does not exist.

- [ ] **Step 3: Implement the session gate and held-fund loop**

Create:

```python
MORNING = (time(9, 30), time(11, 30))
AFTERNOON = (time(13, 0), time(15, 0))


def is_intraday_refresh_time(now: datetime, holidays: frozenset[date]) -> bool:
    local = now.astimezone(SHANGHAI)
    if is_skipped_reminder_day(local.date(), holidays):
        return False
    current = local.time().replace(tzinfo=None)
    return MORNING[0] <= current <= MORNING[1] or AFTERNOON[0] <= current <= AFTERNOON[1]


@dataclass(frozen=True)
class IntradayJobResult:
    status: str
    attempted: int
    succeeded: int
    failed: int


class IntradayRefreshJob:
    def run_scheduled(self) -> IntradayJobResult:
        holidays = parse_holidays(self._setting("cn_holidays", "[]"))
        if not is_intraday_refresh_time(self.clock(), holidays):
            return IntradayJobResult("skipped", 0, 0, 0)
        return self.refresh_held_funds()
```

`refresh_held_funds()` must select distinct positive holdings joined to CN fund assets, call `FundPriceService.refresh_with_fallback(code, self.providers, required_quote_type=QuoteType.INTRADAY_ESTIMATE)` for each code, continue after provider failures, commit once at the end, and return counts. Provider instances are injected in tests; the CLI-owned Tiantian provider is closed in `finally`.

Add CLI parsing and dispatch:

```python
market = commands.add_parser("market-refresh")
market.add_argument("--scheduled", action="store_true", required=True)
```

The command exits `0` for `success` and `skipped`, and `1` only when every attempted held fund fails or an internal database error occurs. It prints one compact status line with counts and no upstream response body.

- [ ] **Step 4: Run job and CLI tests**

Run: `python -m pytest tests/market/test_intraday_job.py tests/test_daily_check.py -q`

Expected: PASS, including all existing `daily-check` tests.

- [ ] **Step 5: Commit the scheduled job**

```bash
git add finance_app/market/intraday_job.py finance_app/cli.py tests/market/test_intraday_job.py tests/test_daily_check.py
git commit -m "feat: schedule held fund estimate refreshes"
```

### Task 8: systemd deployment units

**Files:**

- Create: `deploy/finance-market.service`
- Create: `deploy/finance-market.timer`
- Modify: `deploy/install.sh`
- Modify: `tests/deploy/test_artifacts.py`

- [ ] **Step 1: Write failing deployment artifact tests**

```python
def test_market_timer_triggers_every_five_minutes_on_weekdays() -> None:
    timer = read("finance-market.timer")
    service = read("finance-market.service")
    assert "OnCalendar=Mon..Fri *-*-* 09..15:00/5:00" in timer
    assert "Persistent=false" in timer
    assert "finance market-refresh --scheduled" in service
    assert "User=financeapp" in service
    assert "MemoryMax=300M" in service


def test_installer_enables_market_timer() -> None:
    script = read("install.sh")
    assert "finance-market.service" in script
    assert "finance-market.timer" in script
    assert "systemctl enable --now finance-market.timer" in script
```

- [ ] **Step 2: Run deployment tests and verify missing files**

Run: `python -m pytest tests/deploy/test_artifacts.py -q`

Expected: FAIL because the market units are absent.

- [ ] **Step 3: Add hardened units and installer wiring**

Create `deploy/finance-market.timer`:

```ini
[Unit]
Description=Trigger personal finance intraday fund refresh

[Timer]
OnCalendar=Mon..Fri *-*-* 09..15:00/5:00
Persistent=false
Unit=finance-market.service

[Install]
WantedBy=timers.target
```

Create `deploy/finance-market.service` with the same hardening, environment file, working directory, user, group, state write paths, and memory limit as `finance-daily.service`, but use:

```ini
ExecStart=/opt/personal-finance/current/.venv/bin/finance market-refresh --scheduled
```

Copy both units in `deploy/install.sh`, run `systemctl daemon-reload`, and enable the market timer alongside existing services. Rely on systemd's single active oneshot instance to prevent overlapping scheduled refreshes; application session checks still skip lunch, holidays, and closed boundaries.

- [ ] **Step 4: Run deployment tests**

Run: `python -m pytest tests/deploy/test_artifacts.py -q`

Expected: PASS.

- [ ] **Step 5: Commit deployment artifacts**

```bash
git add deploy/finance-market.service deploy/finance-market.timer deploy/install.sh tests/deploy/test_artifacts.py
git commit -m "ops: add intraday fund refresh timer"
```

### Task 9: Documentation and full regression verification

**Files:**

- Modify: `README.md`
- Modify: tests only if a genuine cross-module regression is exposed.

- [ ] **Step 1: Add the operating documentation**

Document these exact facts in the Chinese README:

- `/funds` is the unified fund data center.
- Official NAV updates asset values; intraday estimates never update total assets.
- Tiantian JSONP is the primary estimate source and `efinance` is the estimate fallback.
- `efinance` also backfills official history; analytics run through the `XalphaAdapter` boundary.
- The five available periods and 252-trading-day volatility convention.
- The timer runs every five minutes but the application only fetches during `09:30–11:30` and `13:00–15:00` on configured Chinese trading days.
- `systemctl status finance-market.timer` and `journalctl -u finance-market.service` are the diagnostic commands.
- Manual NAV is the fallback for unavailable official data and can be edited or deleted.
- No stock tracking, prediction, or automatic trading is included.

- [ ] **Step 2: Run formatting and focused suites**

Run:

```bash
python -m ruff check finance_app tests
python -m pytest tests/market tests/web tests/deploy -q
```

Expected: both commands exit `0`.

- [ ] **Step 3: Run the complete test suite**

Run: `python -m pytest -q`

Expected: all tests pass with no failures or errors.

- [ ] **Step 4: Check repository integrity**

Run:

```bash
git diff --check
git status --short
```

Expected: `git diff --check` prints nothing; status lists only the intentional README change before commit.

- [ ] **Step 5: Commit documentation**

```bash
git add README.md
git commit -m "docs: explain fund data center operations"
```

### Task 10: Merge, publish, deploy, and verify

**Files:**

- No source files should change during this task.

- [ ] **Step 1: Record the verified feature commit and confirm a clean worktree**

Run:

```bash
git status --short
git rev-parse HEAD
```

Expected: empty status and one feature commit SHA.

- [ ] **Step 2: Merge the feature branch into local `master`**

From the primary `H:\1A` worktree, fast-forward or merge `feature/personal-finance` into `master` without discarding unrelated user changes. Run the complete test suite on the resulting `master` checkout.

Expected: merge succeeds and all tests pass.

- [ ] **Step 3: Push GitHub**

Run: `git push origin master`

Expected: `origin/master` advances to the verified local `master` SHA.

- [ ] **Step 4: Deploy the exact pushed SHA**

Use the repository's `deploy/install.sh` release workflow over the configured SSH key. Confirm that the release directory is `/opt/personal-finance/releases/<SHA>` and `/opt/personal-finance/current` points to it. Do not alter `/var/lib/personal-finance/data` except through application migrations; this plan introduces no migration.

- [ ] **Step 5: Verify server health and functionality**

Run read-only checks for:

```bash
systemctl is-active finance-app.service
systemctl is-active finance-market.timer
systemctl list-timers finance-market.timer --no-pager
readlink -f /opt/personal-finance/current
curl --fail --silent --show-error http://127.0.0.1:8000/login
```

Then verify through the Tailscale HTTPS URL that login, `/funds`, all four tabs, an existing official total asset value, and mobile layout render correctly. Do not trigger a production manual refresh outside a valid trading window and do not modify existing production holdings or cash data.

- [ ] **Step 6: Report exact evidence**

Report the test count, pushed SHA, deployed release path, active service/timer states, and any external data source that could not be verified because the market was closed or unavailable.
