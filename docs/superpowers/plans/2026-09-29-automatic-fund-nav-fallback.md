# Automatic Fund NAV Fallback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make daily fund valuation automatic-first, with `EastMoneyFundNavProvider` as the primary official-NAV source, `EfinanceAdapter` as the official-NAV fallback, and manual entry only as an explicit recovery path.

**Architecture:** Keep provider-specific parsing in the existing market adapters. Add a small orchestration method around `FundPriceService` that tries providers in order and returns one auditable result per asset. Keep official NAV selection separate from intraday estimates, and refresh an existing daily portfolio snapshot after a successful official quote without changing cash or holding confirmation timestamps.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy, pytest, Ruff, mypy, Alembic, `efinance` 0.5.9.

---

### Task 1: Define fallback result and source-priority contracts

**Files:**
- Modify: `finance_app/market/service.py`
- Modify: `finance_app/portfolio/service.py`
- Test: `tests/market/test_service.py`
- Test: `tests/portfolio/test_service.py`

- [ ] **Step 1: Write failing tests for ordered fallback and official-source priority**

Add a fallback provider stub whose `fetch()` raises `MarketDataError` for the primary and returns a valid `FundNavQuote` for the fallback. Assert that the primary is called first, the fallback is called second, the persisted row has `source == "efinance"`, and the result records both source attempts without creating an intraday quote. Add a snapshot test with same-date manual, `eastmoney`, and `efinance` rows and assert the manual row is selected.

- [ ] **Step 2: Run the focused tests and verify the expected failure**

Run:

```powershell
py -3.14 -m pytest tests/market/test_service.py -k "fallback or priority" tests/portfolio/test_service.py -q
```

Expected: FAIL because the service has no fallback orchestration and snapshot selection only sorts by date/fetch time.

- [ ] **Step 3: Implement the smallest contracts**

Add an immutable `ProviderAttempt`/fallback result structure in `finance_app/market/service.py`. Add `FundPriceService.refresh_with_fallback(fund_code, providers)` that calls providers in order, delegates persistence to the existing `refresh` behavior, returns on the first successful official quote, and records bounded failure summaries when all providers fail. Add a shared source-priority helper in `finance_app/portfolio/service.py` with the order `manual:*`, `eastmoney`, `efinance`, then other sources. Use that priority as the final tie-breaker after valuation date and before fetched time when choosing an eligible official price; never include `quote_type == "intraday_estimate"` in complete portfolio valuation.

- [ ] **Step 4: Run focused tests and refactor only after green**

Run:

```powershell
py -3.14 -m pytest tests/market/test_service.py tests/portfolio/test_service.py -q
```

Expected: all focused tests pass, including existing failure-preservation tests.

- [ ] **Step 5: Commit**

```powershell
git add finance_app/market/service.py finance_app/portfolio/service.py tests/market/test_service.py tests/portfolio/test_service.py
git commit -m "feat: add official nav fallback and source priority"
```

### Task 2: Use the fallback during the daily refresh

**Files:**
- Modify: `finance_app/cli.py:300-350`
- Test: `tests/test_daily_check.py`

- [ ] **Step 1: Write failing daily-refresh tests**

Add tests that monkeypatch `EastMoneyFundNavProvider` and `EfinanceAdapter`, then assert: primary success does not call `efinance`; primary failure calls `efinance`; one failed fund does not stop the next fund; and the returned result includes the successful fallback source. Preserve the provider-close assertion for the HTTP provider.

- [ ] **Step 2: Run the new tests to verify failure**

```powershell
py -3.14 -m pytest tests/test_daily_check.py -k "refresh_prices or fallback" -q
```

Expected: FAIL because `DailyCheck.refresh_prices()` currently constructs only one `FundPriceService` provider.

- [ ] **Step 3: Wire the ordered providers**

Construct `primary = EastMoneyFundNavProvider(clock=self.clock)` and `fallback = EfinanceAdapter(clock=self.clock)`. Call `FundPriceService.refresh_with_fallback()` for each registered fund. Catch only the per-fund market-data failure at the loop boundary, add a bounded `market_price_refresh_failed` audit record, and continue to the next fund. Keep `_backfill_history()` after the official refresh and leave its existing exception isolation intact. Close the primary provider in `finally`; the efinance adapter has no close requirement.

- [ ] **Step 4: Run daily-check tests**

```powershell
py -3.14 -m pytest tests/test_daily_check.py -q
```

Expected: all daily-check tests pass.

- [ ] **Step 5: Commit**

```powershell
git add finance_app/cli.py tests/test_daily_check.py
git commit -m "feat: refresh official nav with efinance fallback"
```

### Task 3: Surface manual recovery without inventing values

**Files:**
- Modify: `finance_app/cli.py`
- Modify: `finance_app/templates/alerts.html` only if an existing alert field needs a label
- Test: `tests/test_daily_check.py`
- Test: `tests/test_web.py`

- [ ] **Step 1: Write failing tests for recovery alerts**

When both official providers fail for a tracked fund, assert that an `Alert` is created with the fund code/name, last valid valuation date if present, and a message containing “手工补录”; assert that no alert is created when either provider succeeds. Assert that the message contains no fabricated price and that incomplete data still produces `WAIT_FOR_DATA`.

- [ ] **Step 2: Run the tests and verify failure**

```powershell
py -3.14 -m pytest tests/test_daily_check.py tests/test_web.py -k "manual or alert or recovery" -q
```

Expected: FAIL because daily refresh currently records only generic source failures and does not create a manual-recovery alert.

- [ ] **Step 3: Implement idempotent recovery alerts**

Add a helper in `DailyCheck` that creates one open `Alert(alert_type="price", severity="warning", ...)` per asset and business date, reusing the existing alert query patterns. Include only asset identity, last good official valuation date, and source failure summaries. Do not include a price when no valid quote exists. Ensure repeated five-minute timer invocations do not duplicate the same alert.

- [ ] **Step 4: Verify focused web and daily behavior**

```powershell
py -3.14 -m pytest tests/test_daily_check.py tests/test_web.py -q
```

Expected: all selected tests pass and the alerts page renders the recovery message.

- [ ] **Step 5: Commit**

```powershell
git add finance_app/cli.py finance_app/templates/alerts.html tests/test_daily_check.py tests/test_web.py
git commit -m "feat: prompt manual nav recovery after source failures"
```

### Task 4: Recompute the current portfolio snapshot after automatic NAV writes

**Files:**
- Modify: `finance_app/market/service.py`
- Modify: `finance_app/cli.py`
- Modify: `finance_app/portfolio/service.py`
- Test: `tests/market/test_service.py`
- Test: `tests/portfolio/test_service.py`

- [ ] **Step 1: Write the regression test**

Create an existing current-day `PortfolioSnapshot` with confirmed cash and holdings, update a formal NAV through the automatic service, and assert that `total_value_cents` changes immediately. Add a second case with missing cash confirmation and assert that the total remains `None` after the NAV update.

- [ ] **Step 2: Run the regression test and verify failure**

```powershell
py -3.14 -m pytest tests/market/test_service.py tests/portfolio/test_service.py -k "snapshot or revalu" -q
```

Expected: FAIL because only the web manual-price route currently triggers snapshot refresh.

- [ ] **Step 3: Reuse the existing safe refresh helper**

After a successful official quote is flushed, call `refresh_current_snapshot_after_price_update()` with the same clock timestamp. Carry forward only the existing snapshot’s confirmation timestamps. Keep quote writes and snapshot updates in the caller’s transaction so a failed refresh rolls back together with the quote.

- [ ] **Step 4: Run portfolio and market tests**

```powershell
py -3.14 -m pytest tests/market tests/portfolio -q
```

Expected: all market and portfolio tests pass.

- [ ] **Step 5: Commit**

```powershell
git add finance_app/market/service.py finance_app/cli.py finance_app/portfolio/service.py tests/market/test_service.py tests/portfolio/test_service.py
git commit -m "fix: revalue portfolio after automatic nav refresh"
```

### Task 5: Verify source labels and manual fallback UX

**Files:**
- Modify: `finance_app/web/viewmodels.py` if source selection is duplicated there
- Modify: `finance_app/templates/holding_table.html` if the source label needs explicit official-NAV wording
- Test: `tests/test_web.py`

- [ ] **Step 1: Add view-model tests**

Assert that a manual same-day correction is displayed over automatic rows, that official `efinance` is shown as a formal NAV source, and that an intraday estimate is displayed only as an estimate and never marked as a complete priced holding.

- [ ] **Step 2: Implement the shared selection rule**

Use the same source-priority and official-quote filter as the portfolio service. Preserve source, valuation date, fetched time, and freshness labels in the holding view. Do not change missing-value rendering from “待确认/未确认”.

- [ ] **Step 3: Run web tests**

```powershell
py -3.14 -m pytest tests/test_web.py tests/web/test_forms.py -q
```

Expected: all web tests pass.

- [ ] **Step 4: Commit**

```powershell
git add finance_app/web/viewmodels.py finance_app/templates/holding_table.html tests/test_web.py
git commit -m "feat: label official and estimated fund prices clearly"
```

### Task 6: Full release verification, push, deployment, and acceptance

**Files:**
- Modify: `README.md` and `docs/operations.md` only if the automatic fallback workflow is not documented
- Test: full repository test suite and release checks

- [ ] **Step 1: Run complete verification**

```powershell
py -3.14 -m pytest -q
py -3.14 -m ruff check finance_app tests
py -3.14 -m mypy finance_app
git diff --check
```

Expected: zero test failures, zero Ruff errors, zero mypy errors, and no whitespace errors.

- [ ] **Step 2: Commit documentation updates if needed**

```powershell
git add README.md docs/operations.md
git commit -m "docs: explain automatic nav fallback"
```

- [ ] **Step 3: Publish the verified tree**

Create a release commit based on the current GitHub `main`, push it to `origin/main`, and record the exact SHA. Do not force-push or rewrite the production database.

- [ ] **Step 4: Deploy without overwriting state**

Upload a Git archive of the release SHA to the server, install the release using the existing deployment layout, reuse the already-installed virtual environment if package downloads are unavailable, run Alembic from `/opt/personal-finance/current`, and restart only the application/timer services. Preserve `/var/lib/personal-finance` and `/etc/personal-finance/finance.env`.

- [ ] **Step 5: Verify production**

Run read-only checks for the current release path, migration `0004 (head)`, service states, Tailscale state, and `curl --fail http://127.0.0.1/health`. Verify the deployed source contains the fallback method and that no production data was changed.

- [ ] **Step 6: Commit deployment verification notes**

Record the release SHA, test counts, service states, and any unresolved external-source limitations in `docs/deployment-verification-2026-09-29.md`, with no credentials or portfolio values.

