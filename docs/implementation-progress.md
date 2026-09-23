# Implementation Progress

Updated: 2026-09-23

- [x] Task 1: Bootstrap, health route, settings, tests; reviewed and committed.
- [x] Task 2: Database foundation, frozen migrations, UTC persistence; reviewed, 21 tests passing at a626612.
- [ ] Task 3: Authentication and CSRF; implementation running.
- [ ] Task 4: Monthly cash buckets.
- [ ] Task 5: Transactions, reversals and holdings.
- [ ] Task 6: Valuations, freshness and risk rules.
- [ ] Task 7: Responsive dashboard and local preview.
- [ ] Task 8: Manual entry and audit actions.
- [ ] Task 9: CSV/XLSX and OCR candidate imports.
- [ ] Task 10: Fund NAV integration.
- [ ] Task 11: Email and WeChat notifications.
- [ ] Task 12: Daily checks and settings.
- [ ] Task 13: Exports, backups and restore verification.
- [ ] Task 14: Ubuntu deployment artifacts.
- [ ] Task 15: Release checks and operating documentation.
- [ ] Task 16: Server deployment and acceptance.

## Verified Environment

- Local test interpreter: `py -3.14`; default `python` is 3.8 and unsuitable.
- Branch: `feature/personal-finance`.
- SSH access verified; server has about 1.1 GiB available memory and 33 GiB available disk.
- SSH and Alibaba Cloud management/backup services active.
- Finance application not yet deployed.

## External Configuration

Recipient email/service and selected WeChat provider requested asynchronously.
Credentials must be configured privately on the server, never committed.
Current holdings and cash balances require dated user confirmation before live advice.

## Implementation Clarifications

- The 1000-yuan reserve is a monthly contribution, not an automatic reset of accumulated savings.
- Budget allocations do not create cash before actual salary receipt is recorded.
- No investment allocations are assumed approved merely because the application is installed.
- Unknown balances/prices must remain unknown, not silently become zero.
- SQLite INTEGER already supports signed 64-bit values; earlier review's 32-bit claim was incorrect.
