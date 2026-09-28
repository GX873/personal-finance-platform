# Production Deployment Verification - 2026-09-24

## Release

- Deployed commit: `add1954718a1e44d6991b689507f26690c042da6`
- Current release: `/opt/personal-finance/releases/add1954718a1e44d6991b689507f26690c042da6`
- Previous verified release retained: `/opt/personal-finance/releases/473b9c2367144df92f915cc0c8870349be383186`
- Local release gate: 432 tests passed; Ruff and mypy passed.
- Ubuntu `bash -n` passed for `deploy/install.sh` and `scripts/release-check.sh`.

## Server Baseline

- Host: Alibaba Cloud ECS, Ubuntu 24.04.3 LTS, 2 vCPU, 1.6 GiB RAM, 40 GiB disk.
- Available after deployment: about 1.0 GiB RAM and 33 GiB disk.
- `ssh`, `aliyun`, `hbrclient`, and `hbrclientupdater`: active.
- Failed systemd units: none.
- Docker, 1Panel, and a running OpenClaw installation: absent.
- Removed obsolete `/root/openclaw_installer.sh` as part of the previously approved cleanup.

## Application Acceptance

- `finance-app`, Nginx, `finance-daily.timer`, and `finance-backup.timer`: active and enabled.
- Uvicorn listens only on `127.0.0.1:8000`.
- Nginx listens on TCP 80; SSH remains on TCP 22.
- `GET http://127.0.0.1/health`: `{"status":"ok","version":"0.1.0"}`.
- Anonymous `/` request returns a login redirect.
- Initial administrator login succeeded in a localhost-only automated check; the password was not logged.
- `finance daily-check --date 2026-09-24 --dry-run`: `dry-run`; no notifications or persistent finance changes were made.
- Environment file: mode 0600, owned by `financeapp:financeapp`.
- One-time administrator credential file: mode 0600, owned by `root:root`.
- Data, backup, and upload directories: mode 0750, owned by `financeapp:financeapp`.
- Release directory: mode 0755, owned by `root:root`.

## Backup Acceptance

- Online backup service completed successfully.
- Backup directory contains only paired `.sqlite3` and `.sha256` files after the cleanup fix.
- Restore check: `integrity=ok`.
- Verified backup SHA-256: `bc77dcb50798c047a380525ff71bfae261cc5bcf8a4afc74e080a4f26f67235c`.
- Rollback command documented but not executed:

```bash
ln -sfn /opt/personal-finance/releases/473b9c2367144df92f915cc0c8870349be383186 /opt/personal-finance/current
systemctl restart finance-app.service
```

## Pending User-Gated Acceptance

- Alibaba Cloud security group TCP 80 is still closed; external `http://121.41.225.151/health` times out.
- The bootstrap endpoint is HTTP-only. Do not send finance credentials over an untrusted network; TLS remains recommended before broad public exposure.
- Notification delivery is pending a recipient address, SMTP provider, and one WeChat provider. No secret or token is stored in this document.
- Portfolio import is pending the user's explicit preview and totals confirmation. Imported row count is currently zero.
- Current investment cash and current holdings confirmation remain missing, so the system must not issue a definite new-buy recommendation.
