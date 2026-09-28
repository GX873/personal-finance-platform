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

## Tailscale private HTTPS access

The supported remote entry point is Tailnet-only HTTPS through Tailscale
Serve. Nginx and Uvicorn remain bound to loopback. Do not enable Tailscale
Funnel: Funnel publishes a service to the public internet and is outside this
deployment's security boundary.

Install Tailscale from its official Ubuntu 24.04 repository:

```bash
curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/noble.noarmor.gpg \
  -o /usr/share/keyrings/tailscale-archive-keyring.gpg
curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/noble.tailscale-keyring.list \
  -o /etc/apt/sources.list.d/tailscale.list
apt-get update
apt-get install -y tailscale
systemctl enable --now tailscaled
```

Join the server to the owner's personal Tailnet without enabling Tailscale
SSH or accepting routes:

```bash
tailscale up --ssh=false --accept-routes=false --accept-dns=true
```

Open the one-time `https://login.tailscale.com/...` URL printed by the command
only in the owner's browser. Confirm the intended Tailnet before approving the
server. Never paste the login URL, device key, auth key, or Tailnet credentials
into source control, logs, or chat history.

After authorization, enable persistent Tailnet-only HTTPS proxying:

```bash
tailscale serve --bg --https=443 http://127.0.0.1:80
tailscale status
tailscale serve status
tailscale funnel status
```

If Tailscale asks to enable HTTPS certificates, open its admin URL, enable
certificates for this Tailnet, and repeat the `tailscale serve` command. The
Serve status must show the assigned `.ts.net` HTTPS endpoint. The Funnel
status must show that no public service is active.

Before changing Nginx listeners or Alibaba Cloud rules, install Tailscale on
one client, sign in to the same account, open the `.ts.net` address, and verify
the login, dashboard, and settings pages. Then confirm the production boundary:

```bash
nginx -t
ss -lntp | grep -E ':(80|8000)\b'
curl --fail --silent http://127.0.0.1/health
systemctl is-active tailscaled nginx finance-app finance-daily.timer finance-backup.timer
tailscale serve status
tailscale funnel status
```

Nginx must listen only on `127.0.0.1:80` and `[::1]:80`; Uvicorn must listen
only on `127.0.0.1:8000`. Keep `FINANCE_SESSION_HTTPS_ONLY=true` so the login
cookie is never sent over HTTP. From a device disconnected from Tailscale,
verify that the ECS public IP cannot be reached on TCP 80, 443, or 8000. Do not
add public website rules to the Alibaba Cloud security group. Keep the existing
source-restricted TCP 22 rule for maintenance and recovery.

On each computer or phone, install the official Tailscale client, sign in to
the same account, and bookmark the `.ts.net` HTTPS address. No SSH command or
long-running PowerShell window is required for ordinary access. Disconnecting
or signing out of Tailscale must make the site unavailable.

## Tailscale recovery

Before changing the live Nginx configuration, create a root-owned recovery
copy:

```bash
install -o root -g root -m 0644 \
  /etc/nginx/sites-available/personal-finance \
  /etc/nginx/sites-available/personal-finance.pre-tailscale
```

If private HTTPS fails after the change, keep the database and application
state untouched. Disable Serve and restore the previous Nginx file:

```bash
tailscale serve reset
cp /etc/nginx/sites-available/personal-finance.pre-tailscale \
  /etc/nginx/sites-available/personal-finance
nginx -t && systemctl reload nginx
```

Use the existing restricted SSH entry point for recovery. Do not remove
Tailscale state or relax public firewall rules while diagnosing the failure.

## 基金提醒调度

`finance-daily.timer` 每五分钟唤醒一次任务，应用按设置页中的北京时间判断是否到达提醒窗口。默认时间为 14:00；周六、周日及 `cn_holidays` 中配置的日期返回 `not-due`。工资和投资预算周期从每月 15 日开始，到次月 14 日结束。

正式净值与盘中估算分开保存。行情适配器失败时任务降级为 `WAIT_FOR_DATA`，不得用旧估算值生成买入或卖出建议。特殊机会每个工资周期最多记录一次，且不能令储备金低于 900 元。
