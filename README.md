# Personal Finance Platform

This is a single-user, confirmation-gated portfolio ledger. It records salary
and cash buckets, imports holdings, refreshes dated fund NAVs, and produces
condition-based daily advice. It never places trades automatically. Unknown or
stale data stays unknown, so a missing cash confirmation cannot become a buy
signal.

## Local setup

Use Python 3.12 or newer. A virtual environment keeps the application and test
dependencies isolated:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
cp .env.example .env
```

For local development the default SQLite path is `./data/finance.db`. Keep
`.env`, `data/`, `uploads/`, and `backups/` outside the public web root; they
contain credentials or financial records. On a Unix host use:

```bash
install -d -m 700 data uploads backups
chmod 600 .env
```

Initialize the schema and one administrator before starting the server:

```bash
python -m alembic upgrade head
python -m finance_app.cli init-admin --username admin
uvicorn finance_app.app:app --host 127.0.0.1 --port 8000
```

The first command creates tables only. The second prompts for the password
without echoing it. Do not put passwords, SMTP authorization codes, PushPlus
tokens, ServerChan send keys, or WeCom webhook URLs in Git, issue trackers, or
chat. Enter them in the protected environment file on the server.

## Settings and notifications

Non-secret scheduling and allocation settings are managed in the authenticated
Settings page. Notification credentials are environment-only. Supported
providers and variable names are documented in `docs/operations.md`.

The application listens on `127.0.0.1:8000` in the hardened deployment. Nginx
is the only public entry point and denies `/.env`, `/data/`, `/backups/`, and
SQLite files. The initial deployment intentionally uses HTTP only so the
operator can complete a private bootstrap. HTTP sends credentials in cleartext
on an untrusted network; configure a trusted TLS certificate and set
`FINANCE_SESSION_HTTPS_ONLY=true` before exposing the service beyond a private
network. Never treat HTTP-only bootstrap as a production security posture.

## Operations

Read `docs/operations.md` for administrator initialization, daily timer changes,
manual NAV entry, backup retention, restore verification, rollback, and log
inspection. Read `docs/data-import.md` before importing a CSV, XLSX, or OCR
candidate file. Run the release gate before publishing a release:

```bash
bash scripts/release-check.sh
```

The script creates one private temporary directory and removes only that
directory on exit. It runs tests with coverage, static checks, a temporary
Alembic migration, import smoke tests, shell syntax checks, and a secret-pattern
scan. A failing gate is a release blocker.

## Deployment

`deploy/install.sh` targets Ubuntu 24.04 and preserves SSH, Alibaba Cloud
management agents, and backup services. It creates a `financeapp` system user,
private state directories, systemd units, Nginx rules, and daily/backup timers.
The installer does not change Alibaba Cloud security groups; allow TCP 80 (and
later 443) only from the required clients in the Alibaba Cloud console.
