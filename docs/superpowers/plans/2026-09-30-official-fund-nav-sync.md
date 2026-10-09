# Official Fund NAV Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace unreliable intraday fund estimates with an official-NAV-only workflow that synchronizes published NAVs after market close, shows their real valuation dates, and preserves the existing 14:00 advice reminder without implying same-day closing data.

**Architecture:** Keep `PriceSnapshot.quote_type` unchanged so historical `intraday_estimate` rows remain auditable, but remove every runtime producer and UI consumer of those rows. Add a focused `OfficialNavSyncJob` that selects distinct positive CN fund holdings, uses EastMoney first and `efinance` only after a transport/data failure, and persists official NAVs through `FundPriceService`. Run that job from a dedicated CLI command and systemd timer; keep the advice job independent and read-only with respect to market providers.

**Tech Stack:** Python 3.12+, FastAPI, SQLAlchemy, Jinja2, pytest, Ruff, systemd, EastMoney HTTP provider, `efinance==0.5.9` fallback.

---

## Baseline and file map

The dedicated worktree is `H:\1A\personal-finance-platform` on branch `feature/personal-finance`. Before this plan was written, `ruff check .` passed and pytest reported `537 passed, 2 failed`. The two existing failures are the malformed-XLSX exception tests under Python 3.14, where `lxml.etree.XMLSyntaxError` is not converted to `ImportFileError`; they are unrelated to NAV synchronization. Preserve that baseline during targeted work and run the release suite under the production Python 3.12 environment before deployment.

Files and responsibilities after the change:

- `finance_app/market/service.py`: persist valid official NAVs, distinguish provider failure from normal publication lag, and keep fallback ordering deterministic.
- `finance_app/market/nav_job.py`: own held-fund selection, holiday-aware scheduled execution, per-fund isolation, and attempted/succeeded/unchanged/failed counts.
- `finance_app/cli.py`: expose `finance nav-refresh --scheduled`; keep the 14:00 advice command independent from NAV providers.
- `finance_app/web/fund_center.py`, `finance_app/web/viewmodels.py`, `finance_app/web/routes.py`: read and refresh official NAVs only.
- `finance_app/templates/price_form.html`, `finance_app/templates/holding_table.html`, `finance_app/static/forms.css`: present official NAV, valuation date, source, and fetch time without estimate language.
- `deploy/finance-nav.service`, `deploy/finance-nav.timer`, `deploy/install.sh`: install the new schedule, prepare `efinance`'s import directory, and retire `finance-market` units.
- `README.md`, `docs/operations.md`: document the official-NAV schedule and operational checks.
- Delete `finance_app/market/intraday_job.py`, `finance_app/market/tiantian_estimate.py`, `tests/market/test_intraday_job.py`, and `tests/market/test_tiantian_estimate.py`; retain the database enum/value for audit compatibility.

### Task 1: Treat a published official NAV as a successful observation

**Files:**
- Modify: `tests/market/test_service.py`
- Modify: `finance_app/market/service.py`
- Modify: `tests/market/test_adapters.py`
- Modify: `finance_app/market/efinance_adapter.py`
- Modify: `finance_app/market/base.py`

- [ ] **Step 1: Replace stale-publication fallback expectations with official-publication expectations**

In `tests/market/test_service.py`, replace the tests that expect an older official valuation date to force fallback/failure with these cases:

```python
def test_refresh_with_fallback_accepts_latest_published_primary_nav():
    session, _ = setup_session()
    try:
        primary = StubProvider(quote(date(2026, 9, 19)))
        fallback = EfinanceStubProvider(quote=quote(date(2026, 9, 22)))

        result = FundPriceService(
            session, primary, clock=lambda: NOW
        ).refresh_with_fallback("000001", [primary, fallback])

        assert result.status is RefreshStatus.SUCCESS
        assert result.is_fresh is False
        assert result.snapshot is not None
        assert result.snapshot.valuation_date == date(2026, 9, 19)
        assert fallback.calls == []
    finally:
        session.close()


def test_refresh_with_fallback_uses_efinance_only_after_primary_failure():
    session, _ = setup_session()
    try:
        primary = StubProvider(error=RuntimeError("primary unavailable"))
        fallback = EfinanceStubProvider(quote=quote(date(2026, 9, 19)))

        result = FundPriceService(
            session, primary, clock=lambda: NOW
        ).refresh_with_fallback("000001", [primary, fallback])

        assert result.status is RefreshStatus.SUCCESS
        assert result.is_fresh is False
        assert result.snapshot is not None
        assert result.snapshot.source == "efinance"
        assert [attempt.status for attempt in result.provider_attempts] == [
            RefreshStatus.FAILED,
            RefreshStatus.SUCCESS,
        ]
    finally:
        session.close()
```

Also keep the existing tests for wrong quote type, idempotency, all-provider exceptions, and current-snapshot revaluation.

- [ ] **Step 2: Run the two changed service tests and verify the old behavior fails**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/market/test_service.py -k "latest_published or efinance_only" -v
```

Expected: the primary older NAV is currently reported as `STALE`, and the fallback is called, so at least one test fails.

- [ ] **Step 3: Make publication age metadata, not provider failure**

In `FundPriceService.refresh()`, keep `fresh = nav_is_fresh(...)` for downstream risk metadata, but return success for every valid, persisted official observation:

```python
return RefreshResult(
    status=RefreshStatus.SUCCESS,
    is_fresh=fresh,
    snapshot=snapshot,
    last_good_snapshot=snapshot,
    last_good_price=snapshot.price,
    source=quote.source,
    source_url=quote.source_url,
    fetched_at=quote.fetched_at,
    attempts=quote.attempts,
    error_summary=None,
)
```

Do not manufacture a newer `valuation_date`. `refresh_with_fallback()` must stop on the first valid official result and only advance to the next provider after `FAILED` or an unexpected quote type.

- [ ] **Step 4: Remove the runtime estimate-only adapter surface**

Delete `EfinanceEstimateAdapter` from `finance_app/market/efinance_adapter.py` and `IntradayEstimateProvider` from `finance_app/market/base.py`. Preserve `QuoteType.INTRADAY_ESTIMATE` because old rows and migrations still reference its stored value. Remove estimate-adapter assertions/imports from `tests/market/test_adapters.py`; keep coverage for `EfinanceAdapter.fetch()`, `fetch_history()`, and `fetch_positions()`.

- [ ] **Step 5: Run service and adapter tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/market/test_service.py tests/market/test_adapters.py -v
```

Expected: all selected tests pass; official refresh remains idempotent and never accepts an `intraday_estimate` as official data.

- [ ] **Step 6: Commit the official publication semantics**

```powershell
git add finance_app/market/base.py finance_app/market/efinance_adapter.py finance_app/market/service.py tests/market/test_adapters.py tests/market/test_service.py
git commit -m "fix: accept latest published official nav"
```

### Task 2: Add the independent held-fund official NAV synchronization job

**Files:**
- Create: `finance_app/market/nav_job.py`
- Create: `tests/market/test_nav_job.py`
- Modify: `finance_app/cli.py`
- Delete: `finance_app/market/intraday_job.py`
- Delete: `finance_app/market/tiantian_estimate.py`
- Delete: `tests/market/test_intraday_job.py`
- Delete: `tests/market/test_tiantian_estimate.py`

- [ ] **Step 1: Write job tests for scope, fallback, idempotency, failure isolation, and holidays**

Create `tests/market/test_nav_job.py` using the existing `_seed_refresh_scope` fixture pattern. The central assertions must be:

```python
def test_job_refreshes_distinct_positive_held_cn_funds(db_session):
    first, second = seed_refresh_scope(db_session)
    primary = PerCodeProvider()

    result = OfficialNavSyncJob(
        db_session,
        providers=[primary],
        clock=lambda: EVENING_NOW,
    ).run_scheduled()

    assert primary.calls == [first.code, second.code]
    assert result == NavSyncResult(
        status="success", attempted=2, succeeded=2, unchanged=0, failed=0
    )
    assert {
        row.quote_type for row in db_session.scalars(select(PriceSnapshot))
    } == {QuoteType.OFFICIAL_NAV.value}


def test_job_counts_repeat_observation_as_unchanged(db_session):
    first, _ = seed_refresh_scope(db_session, include_second=False)
    provider = PerCodeProvider()
    job = OfficialNavSyncJob(
        db_session, providers=[provider], clock=lambda: EVENING_NOW
    )

    assert job.refresh_held_funds().succeeded == 1
    second = job.refresh_held_funds()

    assert second.unchanged == 1
    assert db_session.scalar(select(func.count()).select_from(PriceSnapshot)) == 1


def test_evening_holiday_is_skipped_but_morning_backfill_runs(db_session):
    seed_refresh_scope(db_session)
    db_session.add(AppSetting(key="cn_holidays", value='["2026-10-01"]'))
    db_session.commit()

    evening = OfficialNavSyncJob(
        db_session,
        providers=[PerCodeProvider()],
        clock=lambda: datetime.fromisoformat("2026-10-01T20:00:00+08:00"),
    ).run_scheduled()
    morning = OfficialNavSyncJob(
        db_session,
        providers=[PerCodeProvider()],
        clock=lambda: datetime.fromisoformat("2026-10-02T08:00:00+08:00"),
    ).run_scheduled()

    assert evening.status == "skipped"
    assert morning.status == "success"
```

Add a per-code failing provider case that proves one failure does not stop the next code, plus a primary-failure/fallback-success case.

- [ ] **Step 2: Write CLI boundary tests**

In the same file, test `main(["nav-refresh", "--scheduled"])` with injected `EastMoneyFundNavProvider`, `EfinanceAdapter`, and `OfficialNavSyncJob`. Assert the primary HTTP provider is closed and output is exactly:

```text
nav-refresh status=success attempted=2 succeeded=1 unchanged=1 failed=0
```

For an unexpected exception, assert exit code `1`, no sensitive exception text, and stderr exactly:

```text
nav-refresh status=failed error=internal_error
```

- [ ] **Step 3: Run the new test module and verify imports fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/market/test_nav_job.py -v
```

Expected: collection fails because `finance_app.market.nav_job` does not exist.

- [ ] **Step 4: Implement the job with a compact immutable result**

Create `finance_app/market/nav_job.py` with this public structure:

```python
@dataclass(frozen=True)
class NavSyncResult:
    status: str
    attempted: int
    succeeded: int
    unchanged: int
    failed: int


class OfficialNavSyncJob:
    def __init__(self, session, *, providers, clock=utc_now):
        if not providers:
            raise ValueError("at least one official NAV provider is required")
        self.session = session
        self.providers = list(providers)
        self.clock = clock

    def run_scheduled(self) -> NavSyncResult:
        local = self.clock().astimezone(SHANGHAI)
        holidays = parse_holidays(self._setting("cn_holidays", "[]"))
        if local.hour >= 12 and is_skipped_reminder_day(local.date(), holidays):
            return NavSyncResult("skipped", 0, 0, 0, 0)
        return self.refresh_held_funds()
```

`refresh_held_funds()` must select distinct `Asset.code` values joined to `Holding` with `Holding.quantity > 0`, `Asset.market == "CN"`, and `Asset.asset_class == "fund"`. Before each refresh, capture the IDs of matching official snapshots. Call:

```python
result = service.refresh_with_fallback(
    code,
    self.providers,
    required_quote_type=QuoteType.OFFICIAL_NAV,
)
```

Count `SUCCESS` with a pre-existing returned snapshot ID as `unchanged`; count a new snapshot as `succeeded`; count other results as `failed`. Commit once after the loop. Return overall `failed` only when at least one code was attempted and every code failed.

- [ ] **Step 5: Replace the CLI command and remove obsolete estimate modules**

In `finance_app/cli.py`, remove imports of `EfinanceEstimateAdapter`, `IntradayRefreshJob`, and `TiantianEstimateProvider`; import `OfficialNavSyncJob`. Replace the parser and command branch with:

```python
nav = commands.add_parser("nav-refresh")
nav.add_argument("--scheduled", action="store_true", required=True)
```

and:

```python
if args.command == "nav-refresh":
    primary = None
    try:
        with get_session_factory()() as db:
            primary = EastMoneyFundNavProvider()
            result = OfficialNavSyncJob(
                db, providers=[primary, EfinanceAdapter()]
            ).run_scheduled()
    except Exception:  # noqa: BLE001 - sanitize the CLI boundary
        print("nav-refresh status=failed error=internal_error", file=sys.stderr)
        return 1
    finally:
        if primary is not None:
            primary.close()
    print(
        f"nav-refresh status={result.status} attempted={result.attempted} "
        f"succeeded={result.succeeded} unchanged={result.unchanged} "
        f"failed={result.failed}"
    )
    return 1 if result.status == "failed" else 0
```

Delete the four obsolete intraday files listed above. Do not delete old database rows or alter migrations.

- [ ] **Step 6: Run the new job/CLI tests and market tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/market/test_nav_job.py tests/market/test_service.py tests/market/test_adapters.py -v
```

Expected: all selected tests pass and no test imports a runtime intraday provider.

- [ ] **Step 7: Commit the independent sync job**

```powershell
git add -A finance_app/market finance_app/cli.py tests/market
git commit -m "feat: add official nav sync job"
```

### Task 3: Decouple the 14:00 advice reminder and label NAV valuation dates

**Files:**
- Modify: `tests/test_daily_check.py`
- Modify: `finance_app/cli.py`

- [ ] **Step 1: Add a no-provider-call reminder test**

Add a scheduled daily-check test that seeds stored official NAV, cash, holdings, and a valid monthly budget, replaces both provider constructors with functions that raise `AssertionError`, runs `DailyCheck.run(business_date)`, and asserts the reminder still reaches `SUCCESS`. This proves the advice workflow reads the database and cannot be blocked by NAV synchronization.

- [ ] **Step 2: Add valuation-date digest coverage**

Extend the digest fixture with official snapshots dated `2026-09-22` while the business date is `2026-09-23`, then assert:

```python
notification = check.build_digest(business_date, snapshot, freshness, advice)
assert "官方净值日期：2026-09-22" in notification.body
assert "今日收盘净值" not in notification.body
assert "请手工补录" not in notification.body
```

For multiple official valuation dates, assert the body renders `官方净值日期：2026-09-21 至 2026-09-22`.

- [ ] **Step 3: Run focused tests and verify the current reminder still refreshes providers**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_daily_check.py -k "provider_call or valuation_date" -v
```

Expected: the no-provider-call test fails because `run()` currently calls `refresh_prices()`, and the date assertion fails because freshness does not expose valuation dates.

- [ ] **Step 4: Remove market fetching from the advice execution path**

Remove `self.refresh_prices(business_date)` from `DailyCheck.run()`. Remove the now-unused automatic refresh/backfill/manual-recovery methods only after `rg` confirms no other caller. The advice job must continue to validate stored rows, create a snapshot, evaluate rules, and send notifications at the configured time.

- [ ] **Step 5: Carry official valuation dates into the digest**

In `validate_freshness()`, add:

```python
"nav_dates": sorted({row.valuation_date for row in price_rows}),
```

In `build_digest()`, format those dates without claiming they are the business date:

```python
nav_dates = summary_freshness.get("nav_dates", [])
if not nav_dates:
    nav_date_text = "未知"
elif len(nav_dates) == 1:
    nav_date_text = nav_dates[0].isoformat()
else:
    nav_date_text = f"{nav_dates[0].isoformat()} 至 {nav_dates[-1].isoformat()}"

lines = [
    f"总资产：{total}",
    f"现金桶：{bucket_text}",
    f"官方净值日期：{nav_date_text}",
    f"数据时间：{domain_times}",
    f"数据来源：{source_text}",
]
```

- [ ] **Step 6: Run the complete daily-check tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_daily_check.py -v
```

Expected: all tests pass; the reminder contains the stored official valuation date and performs no market HTTP request.

- [ ] **Step 7: Commit reminder decoupling**

```powershell
git add finance_app/cli.py tests/test_daily_check.py
git commit -m "fix: decouple advice from nav fetching"
```

### Task 4: Make every website surface official-NAV-only

**Files:**
- Modify: `tests/web/test_fund_center.py`
- Modify: `tests/web/test_forms.py`
- Modify: `tests/test_web.py`
- Modify: `finance_app/web/fund_center.py`
- Modify: `finance_app/web/viewmodels.py`
- Modify: `finance_app/web/routes.py`
- Modify: `finance_app/templates/price_form.html`
- Modify: `finance_app/templates/holding_table.html`
- Modify: `finance_app/static/forms.css`

- [ ] **Step 1: Add view-model tests that old estimates are invisible**

In `tests/web/test_fund_center.py`, seed a newer `intraday_estimate` beside an older official snapshot and assert the serialized fund has only these quote fields:

```python
assert selected["official_nav"] == "1.20000000"
assert selected["official_date"] == "2026-09-29"
assert selected["data_source"] == "eastmoney"
assert selected["official_fetched_at"] is not None
assert "intraday_estimate" not in selected
assert "estimated_profit_cents" not in selected
assert all(point["quote_type"] == "official_nav" for point in vm["history"])
```

In `tests/test_web.py`, replace `test_intraday_estimate_is_reference_only` with a test asserting an estimate-only holding renders `nav == "待补充"`, `priced is False`, and contributes zero to totals.

- [ ] **Step 2: Change form/page expectations to official language**

In `tests/web/test_forms.py`, assert `/funds` contains `最新官方净值`, `官方净值日期`, `数据来源`, `最近获取时间`, and `获取最新官方净值`. Assert it does not contain `盘中估值`, `估算涨跌`, `估算盈亏`, or `刷新行情`.

Update the manual refresh tests so injected providers are `EastMoneyFundNavProvider` and `EfinanceAdapter`, the service receives `required_quote_type=QuoteType.OFFICIAL_NAV`, CSRF and cooldown behavior remain unchanged, and the primary HTTP provider is closed.

- [ ] **Step 3: Run the focused web tests and verify they fail on estimate fields**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/web/test_fund_center.py tests/web/test_forms.py tests/test_web.py -k "fund or nav or estimate or refresh" -v
```

Expected: failures reference the current estimate fields, estimate text, and estimate-only dashboard fallback.

- [ ] **Step 4: Remove estimate reads from both view models**

In `finance_app/web/fund_center.py`, remove the `_latest_quote(...INTRADAY_ESTIMATE...)` call and all `intraday_*`/`estimated_*` dictionary fields. Keep `_official_history()` and official-only metrics unchanged.

In `finance_app/web/viewmodels.py`, select only `PriceSnapshot.quote_type == "official_nav"`; remove fallback to `intraday_estimate`, the `estimate` flag, and estimate-specific display state. An estimate-only asset must remain unpriced rather than showing a reference value.

- [ ] **Step 5: Change manual refresh to the official provider chain**

Rename `refresh_fund_estimate()` to `refresh_fund_official_nav()`. Preserve `_require_held_cn_fund`, `_claim_manual_refresh`, POST, CSRF, audit event, and cooldown. Construct:

```python
primary = EastMoneyFundNavProvider(clock=utc_now)
try:
    result = FundPriceService(db, primary, clock=utc_now).refresh_with_fallback(
        asset.code,
        [primary, EfinanceAdapter(clock=utc_now)],
        required_quote_type=QuoteType.OFFICIAL_NAV,
    )
    # existing bounded audit and commit
finally:
    primary.close()
```

Keep `success` for any valid latest published official NAV even when its valuation date is before today.

- [ ] **Step 6: Simplify the templates and styles**

In `price_form.html`:

- change subtitle to `查看官方净值、历史走势与持仓风险。`;
- render one quote summary with labels `最新官方净值` and `官方净值日期`;
- show `数据来源` and `最近获取时间`;
- change the POST button to `获取最新官方净值`;
- on success say `已获取上游最新官方净值，实际净值日期见下方。`;
- on failure say `暂时未获取到官方净值，已保留最近有效数据。`;
- remove estimate columns from holdings and the manual summary.

In `holding_table.html`, replace the status branch with `官方净值日期 {{ h.date }}` when a price exists and `暂无官方净值` otherwise. In `forms.css`, remove `.quote-estimate` rules and reduce `.fund-holdings-table` minimum width to match the remaining columns.

- [ ] **Step 7: Run all web tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/web tests/test_web.py -v
```

Expected: all selected tests pass, historical estimate rows are never displayed, and manual refresh security behavior is unchanged.

- [ ] **Step 8: Commit the official-only interface**

```powershell
git add finance_app/web finance_app/templates finance_app/static/forms.css tests/web tests/test_web.py
git commit -m "feat: show official fund nav only"
```

### Task 5: Replace the intraday systemd units with official NAV scheduling

**Files:**
- Create: `deploy/finance-nav.service`
- Create: `deploy/finance-nav.timer`
- Delete: `deploy/finance-market.service`
- Delete: `deploy/finance-market.timer`
- Modify: `deploy/install.sh`
- Modify: `tests/deploy/test_artifacts.py`

- [ ] **Step 1: Replace deployment artifact tests**

Replace market-timer assertions with:

```python
def test_nav_timer_runs_after_close_and_next_morning() -> None:
    timer = read("finance-nav.timer")
    service = read("finance-nav.service")
    assert "OnCalendar=Mon..Fri *-*-* 18,20,22:00:00" in timer
    assert "OnCalendar=Tue..Sat *-*-* 08:00:00" in timer
    assert "Persistent=true" in timer
    assert "finance nav-refresh --scheduled" in service
    assert "User=financeapp" in service
    assert "ProtectSystem=strict" in service
    assert "MemoryMax=300M" in service


def test_installer_retires_market_timer_and_prepares_efinance() -> None:
    script = read("install.sh")
    assert "finance-nav.service" in script
    assert "finance-nav.timer" in script
    assert "systemctl enable --now finance-nav.timer" in script
    assert "systemctl disable --now finance-market.timer" in script
    assert 'rm -f /etc/systemd/system/finance-market.service' in script
    assert 'rm -f /etc/systemd/system/finance-market.timer' in script
    assert "site-packages/efinance/data" in script
```

Also assert `deploy/finance-market.service` and `.timer` no longer exist.

- [ ] **Step 2: Run artifact tests and verify the new files are missing**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/deploy/test_artifacts.py -v
```

Expected: failures report missing `finance-nav` units and the old installer behavior.

- [ ] **Step 3: Add the new service and timer**

Create `deploy/finance-nav.service`:

```ini
[Unit]
Description=Personal finance official fund NAV synchronization
After=network-online.target finance-app.service
Wants=network-online.target

[Service]
Type=oneshot
User=financeapp
Group=financeapp
WorkingDirectory=/opt/personal-finance/current
EnvironmentFile=/etc/personal-finance/finance.env
ExecStart=/opt/personal-finance/current/.venv/bin/finance nav-refresh --scheduled
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/personal-finance/data
MemoryMax=300M
UMask=0077
```

Create `deploy/finance-nav.timer`:

```ini
[Unit]
Description=Trigger personal finance official fund NAV synchronization

[Timer]
OnCalendar=Mon..Fri *-*-* 18,20,22:00:00
OnCalendar=Tue..Sat *-*-* 08:00:00
Persistent=true
Unit=finance-nav.service

[Install]
WantedBy=timers.target
```

- [ ] **Step 4: Update the installer and prepare the immutable package directory**

After installing the release dependencies and before making the release root-owned, create the directory that `efinance` tries to create during import:

```bash
EFINANCE_DATA_DIR="$RELEASE_DIR/.venv/lib/python3.12/site-packages/efinance/data"
install -d -o root -g root -m 0755 "$EFINANCE_DATA_DIR"
```

Install only `finance-nav.service` and `finance-nav.timer` in the unit loop. Before `daemon-reload`, retire old units idempotently:

```bash
systemctl disable --now finance-market.timer 2>/dev/null || true
systemctl stop finance-market.service 2>/dev/null || true
rm -f /etc/systemd/system/finance-market.service
rm -f /etc/systemd/system/finance-market.timer
```

After `daemon-reload`, use `systemctl enable --now finance-nav.timer`. Do not add writable access to the release directory and do not change SSH, Nginx, Tailscale, email, or state-directory permissions.

- [ ] **Step 5: Run artifact tests and shell syntax validation**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/deploy/test_artifacts.py -v
bash -n deploy/install.sh
```

Expected: all deployment tests pass and `bash -n` exits `0` without output.

- [ ] **Step 6: Commit deployment scheduling**

```powershell
git add -A deploy tests/deploy/test_artifacts.py
git commit -m "deploy: schedule official nav synchronization"
```

### Task 6: Update documentation and run release verification

**Files:**
- Modify: `README.md`
- Modify: `docs/operations.md`
- Modify: `docs/implementation-progress.md`
- Create: `docs/deployment-verification-2026-09-30.md`

- [ ] **Step 1: Replace obsolete operational language**

Document that:

- only official NAV participates in totals, charts, risk metrics, and reminders;
- EastMoney is primary and `efinance` is fallback only after provider failure;
- an older valuation date is normal until the fund company publishes a newer official NAV;
- synchronization runs Monday-Friday at 18:00, 20:00, 22:00 and Tuesday-Saturday at 08:00 Beijing time;
- the 14:00 advice reminder uses the latest already-published official NAV and prints its valuation date;
- manual NAV remains an exceptional correction path;
- operators inspect `finance-nav.timer` and `journalctl -u finance-nav.service`.

Remove instructions for `finance-market.timer`, Tiantian JSONP, five-minute estimate refresh, and estimate-only display.

- [ ] **Step 2: Scan all active product surfaces for estimate wording**

Run:

```powershell
rg -n "盘中估值|估算涨跌|估算盈亏|finance-market|market-refresh|TiantianEstimateProvider|EfinanceEstimateAdapter|IntradayRefreshJob" finance_app deploy tests README.md docs/operations.md docs/implementation-progress.md
```

Expected: no matches. Matches in historical specs/plans and database migration tests are permitted because they document or preserve audit history.

- [ ] **Step 3: Run formatting, focused tests, and the complete local suite**

Run:

```powershell
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\python.exe -m pytest tests/market tests/web tests/deploy tests/test_daily_check.py tests/test_web.py -q
.\.venv\Scripts\python.exe -m pytest -q
git diff --check
```

Expected: Ruff, focused tests, and `git diff --check` pass. On local Python 3.14, the complete suite may retain only the two documented malformed-XLSX baseline failures; no NAV-related test may fail. Run the complete suite again under Ubuntu 24.04/Python 3.12 before deployment and require all tests to pass there.

- [ ] **Step 4: Commit documentation and verification preparation**

```powershell
git add README.md docs/operations.md docs/implementation-progress.md docs/deployment-verification-2026-09-30.md
git commit -m "docs: document official nav operations"
```

### Task 7: Merge, push, back up, deploy, and verify production

**Files:**
- Update after verification: `docs/deployment-verification-2026-09-30.md`

- [ ] **Step 1: Verify branch state and merge into local master**

From `H:\1A\personal-finance-platform`:

```powershell
git status --short
git log --oneline --decorate -8
```

Expected: the feature worktree is clean and includes the design plus implementation commits. In the primary worktree, preserve unrelated user files, then fast-forward `master`:

```powershell
git -C H:\1A merge --ff-only feature/personal-finance
git -C H:\1A rev-parse HEAD
```

- [ ] **Step 2: Push the exact master commit to GitHub**

```powershell
git -C H:\1A push origin master
git -C H:\1A ls-remote origin refs/heads/master
git -C H:\1A rev-parse HEAD
```

Expected: the remote `refs/heads/master` SHA exactly equals local `master`. Do not force-push and do not include financial data, screenshots, credentials, or private keys.

- [ ] **Step 3: Create and restore-check a production backup before installation**

Use the existing approved key and load `/etc/personal-finance/finance.env` through systemd/CLI without printing it. On the server:

```bash
cd /opt/personal-finance/current
backup_path="$(sudo -u financeapp .venv/bin/finance backup --directory /var/lib/personal-finance/backups --keep 14)"
sudo -u financeapp .venv/bin/finance restore-check "$backup_path"
```

Expected: restore-check reports `integrity=ok` and a SHA-256 checksum. Record the backup path in the verification document, but never copy the database into the repository.

- [ ] **Step 4: Publish and install the exact GitHub commit**

Fetch `master` into a temporary server-side checkout, verify its SHA equals the value from Step 2, and run:

```bash
RELEASE_SHA="$(git rev-parse HEAD)" bash deploy/install.sh
cd /opt/personal-finance/current
sudo -u financeapp .venv/bin/python -m alembic upgrade head
systemctl restart finance-app.service
```

Expected: `/opt/personal-finance/current` resolves to `/opt/personal-finance/releases/<exact-master-sha>`. The existing `/var/lib/personal-finance`, `/etc/personal-finance/finance.env`, Nginx, Tailscale, SSH, and email configuration remain unchanged.

- [ ] **Step 5: Verify Python 3.12, database integrity, services, timers, and `efinance` import**

Run on production:

```bash
cd /opt/personal-finance/current
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
sudo -u financeapp .venv/bin/python -c 'import efinance; print("efinance-import-ok")'
sqlite3 /var/lib/personal-finance/data/finance.db 'PRAGMA integrity_check;'
systemctl is-active finance-app.service finance-daily.timer finance-backup.timer finance-nav.timer nginx tailscaled
systemctl is-enabled finance-daily.timer finance-backup.timer finance-nav.timer
systemctl list-timers finance-nav.timer --all --no-pager
systemctl status finance-market.timer --no-pager
```

Expected: all tests and Ruff pass under Python 3.12; import prints `efinance-import-ok`; SQLite prints `ok`; current services/timers are active and enabled; the retired market timer reports that the unit cannot be found.

- [ ] **Step 6: Run one official NAV synchronization and inspect bounded output**

```bash
systemctl start finance-nav.service
systemctl status finance-nav.service --no-pager
journalctl -u finance-nav.service -n 30 --no-pager
```

Expected: the oneshot exits successfully and prints attempted/succeeded/unchanged/failed counts. Re-running it must not increase official snapshot row count for the same source and valuation date. It must not create new `intraday_estimate` rows or modify holdings, costs, cash, or transactions.

- [ ] **Step 7: Verify the private website and official-only fund page**

Run server-local health checks:

```bash
curl --fail --silent http://127.0.0.1/health
curl --head --silent http://127.0.0.1/login
tailscale serve status
```

Then open `https://izbp149gio34hhta60ahduz.tail8b5d84.ts.net` from the owner's Tailnet, log in, and verify the fund page shows official NAV, valuation date, source, and fetch time with no estimate fields. Confirm total assets still use the same holdings/cash and that email settings are unchanged.

- [ ] **Step 8: Record final evidence and push the verification note**

Update `docs/deployment-verification-2026-09-30.md` with the exact commit SHA, backup restore-check result, release path, pytest/Ruff results, SQLite integrity result, timer schedule, service states, and website checks. Commit and push:

```powershell
git add docs/deployment-verification-2026-09-30.md
git commit -m "docs: record official nav deployment"
git push origin master
```

The documentation-only final commit does not require another application reinstall; verify GitHub `master` contains it and the recorded deployed application SHA remains the preceding implementation commit.
