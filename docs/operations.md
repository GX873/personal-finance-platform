# Operations Runbook

This runbook assumes Ubuntu 24.04 and the deployment layout created by
`deploy/install.sh`. Commands are run as `root` or with `sudo` unless noted.
The application user is `financeapp`; it has no login shell.

## First bootstrap and administrator

1. Confirm the source release and review `deploy/nginx-finance.conf`.
2. Run the installer once. It creates `/etc/personal-finance/finance.env` with
   mode `0600`, `/var/lib/personal-finance/{data,backups,uploads}` with mode
   `0750`, and `/var/log/personal-finance` with mode `0750`.
3. Review the generated environment file and add notification settings without
   committing it. The database URL should remain under `/var/lib/personal-finance`.
4. Run the migration and initialize the only administrator:

   ```bash
   cd /opt/personal-finance/current
   sudo -u financeapp .venv/bin/python -m alembic upgrade head
   sudo -u financeapp .venv/bin/finance init-admin --username admin
   systemctl enable --now finance-app.service
   ```

   `init-admin` refuses to create a second active administrator. Use
   `change-password --username admin` after a password reset request. Keep the
   password out of shell history by accepting the interactive prompt.

The service binds only to `127.0.0.1:8000`; Nginx serves the public endpoint.
Do not expose Uvicorn directly or change its bind address to `0.0.0.0`.

## Environment and notification channels

Set only the provider you intend to use. Restart the app after editing the
environment file. Secret values are never accepted by the Settings form and
must not be copied into logs.

Email via SMTP:

```ini
FINANCE_SMTP_HOST=smtp.example.com
FINANCE_SMTP_PORT=587
FINANCE_SMTP_SECURITY=starttls
FINANCE_SMTP_USERNAME=finance@example.com
FINANCE_SMTP_AUTHORIZATION_CODE=<provider-app-password>
FINANCE_SMTP_SENDER=finance@example.com
FINANCE_SMTP_RECIPIENT=owner@example.com
```

For implicit TLS use `FINANCE_SMTP_SECURITY=ssl` and the provider's SSL port.
Use an app password or authorization code, not a personal mailbox password.

WeChat-compatible providers:

```ini
FINANCE_PUSHPLUS_TOKEN=<pushplus-token>
FINANCE_SERVERCHAN_SENDKEY=<serverchan-sendkey>
FINANCE_WECOM_WEBHOOK_URL=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=<key>
```

The application validates the official WeCom HTTPS host and does not follow
arbitrary webhook URLs. Enable channels and choose the daily schedule in the
authenticated Settings page; credentials remain environment-only. Delivery is
audited per channel and a previously accepted message is not sent again.

## Daily timer and manual check

`finance-daily.timer` wakes every five minutes. The application enforces the
configured Beijing-time five-minute window, so repeated timer invocations are
idempotent. View status and recent logs with:

```bash
systemctl status finance-daily.timer finance-daily.service
journalctl -u finance-daily.service --since today --no-pager
```

Change the schedule in the Settings page (for example, `09:00`) and keep the
timer cadence unchanged. To modify the cadence itself, edit a release-managed
unit, validate it, reload systemd, and restart the timer:

```bash
systemctl edit finance-daily.timer
# Put an OnCalendar=... override in the drop-in; retain the five-minute cadence.
systemctl daemon-reload
systemctl restart finance-daily.timer
systemctl list-timers finance-daily.timer
```

A one-off check is safe and does not trade:

```bash
sudo -u financeapp .venv/bin/finance daily-check --date 2026-09-24
sudo -u financeapp .venv/bin/finance daily-check --scheduled --dry-run
```

The dry run performs no HTTP requests, notifications, or persistent changes.
Advice is conditional whenever prices, holdings, or cash are missing or stale.

## Manual NAV entry

Use the authenticated Prices page only when a reliable dated source is
available. Enter the fund code, NAV, valuation date, source, and fetched time;
never backdate an observation to hide a stale quote. The EastMoney adapter is
preferred for supported funds. A manually entered value must be reviewed by a
human before the next daily check and remains traceable in the audit log.

## Backups and restore-check

The backup timer runs at 03:30 and retains 14 verified SQLite snapshots. Check
the timer and list backups:

```bash
systemctl status finance-backup.timer finance-backup.service
ls -l /var/lib/personal-finance/backups
journalctl -u finance-backup.service --since yesterday --no-pager
```

Run an explicit backup when preparing a release:

```bash
sudo -u financeapp .venv/bin/finance backup --directory /var/lib/personal-finance/backups --keep 14
```

Before relying on a file, run `restore-check`; it verifies SQLite integrity,
schema compatibility, and the checksum without replacing the live database:

```bash
sudo -u financeapp .venv/bin/finance restore-check /var/lib/personal-finance/backups/latest.sqlite3
```

Keep backups on a private filesystem. A backup is not a substitute for an
off-host copy; use the Alibaba Cloud backup service or another encrypted store
with a retention policy approved by the owner. Never place backups under the
Nginx document root.

## Logs, release rollback, and inspection

Inspect application and Nginx logs without printing environment values:

```bash
journalctl -u finance-app.service -n 100 --no-pager
journalctl -u finance-daily.service -n 100 --no-pager
tail -n 100 /var/log/personal-finance/error.log
```

Notification errors are classified and secrets are redacted. If a log line
contains a token or password, stop sharing it, rotate the credential, and
review access permissions before continuing.

Each release is immutable at `/opt/personal-finance/releases/<commit>`, and
`/opt/personal-finance/current` is a symlink. Roll back only to a release that
passed the release gate and whose migration is compatible:

```bash
readlink -f /opt/personal-finance/current
ls -1dt /opt/personal-finance/releases/*
ln -sfn /opt/personal-finance/releases/<known-good-commit> /opt/personal-finance/current
systemctl restart finance-app.service
systemctl status finance-app.service --no-pager
```

Do not delete the current release during rollback. If a migration has changed
the schema, restore a verified backup first and follow the documented migration
plan; never run an unreviewed downgrade against the only database.

## HTTP-only bootstrap and TLS warning

The installer starts with `FINANCE_SESSION_HTTPS_ONLY=false` because a fresh
host may not have a certificate yet. This is acceptable only on a private,
temporary bootstrap network. HTTP exposes login cookies and credentials to
network observers. Configure TLS termination in Nginx, verify redirects and
certificate renewal, then set `FINANCE_SESSION_HTTPS_ONLY=true`, reload the
environment, and restart the app. Restrict cloud security-group rules to the
needed clients; do not treat an unencrypted public port 80 as finished
deployment.
