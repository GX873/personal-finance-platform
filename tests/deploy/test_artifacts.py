from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[2]
DEPLOY = ROOT / "deploy"


def read(name: str) -> str:
    return (DEPLOY / name).read_text(encoding="utf-8")


def test_linux_deployment_artifacts_are_exported_with_lf_line_endings() -> None:
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "deploy/** text eol=lf" in attributes
    assert "scripts/*.sh text eol=lf" in attributes


def test_application_binds_loopback_and_uses_hardened_service() -> None:
    unit = read("finance-app.service")
    assert "User=financeapp" in unit
    assert "ExecStart=" in unit and "--host 127.0.0.1" in unit
    assert "--port 8000" in unit
    assert "NoNewPrivileges=true" in unit
    assert "PrivateTmp=true" in unit
    assert "ProtectSystem=strict" in unit
    assert "ReadWritePaths=/var/lib/personal-finance/data" in unit
    assert "ReadWritePaths=/var/lib/personal-finance/uploads" in unit
    assert "EnvironmentFile=/etc/personal-finance/finance.env" in unit
    assert "Restart=on-failure" in unit
    assert "MemoryMax=300M" in unit


def test_daily_timer_runs_every_five_minutes_and_consults_saved_schedule() -> None:
    timer = read("finance-daily.timer")
    service = read("finance-daily.service")
    assert "OnCalendar=*-*-* *:0/5:00" in timer
    assert "Persistent=true" in timer
    assert "daily-check --scheduled" in service
    assert "User=financeapp" in service


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


def test_backup_timer_runs_online_backup_as_locked_user() -> None:
    timer = read("finance-backup.timer")
    service = read("finance-backup.service")
    assert "OnCalendar=*-*-* 03:30:00" in timer
    assert "Persistent=true" in timer
    assert "ExecStart=" in service and "finance backup" in service
    assert "User=financeapp" in service
    assert "ReadWritePaths=/var/lib/personal-finance/backups" in service


def test_nginx_proxies_only_loopback_and_blocks_sensitive_files() -> None:
    config = read("nginx-finance.conf")
    assert "listen 127.0.0.1:80 default_server;" in config
    assert "listen [::1]:80 default_server;" in config
    assert not re.search(r"(?m)^\s*listen\s+80(?:\s|;)", config)
    assert not re.search(r"(?m)^\s*listen\s+\[::\]:80(?:\s|;)", config)
    assert "proxy_pass http://127.0.0.1:8000" in config
    assert "client_max_body_size 10m" in config
    assert "location ~ /\\." in config
    assert "location ~* \\.(?:env|sqlite3)(?:$|/)" in config
    assert config.count("deny all") >= 2
    for header in (
        "X-Content-Type-Options",
        "X-Frame-Options",
        "Referrer-Policy",
        "Content-Security-Policy",
    ):
        assert header in config


def test_logrotate_is_private_and_compresses_logs() -> None:
    config = read("logrotate-finance")
    assert "/var/log/personal-finance/*.log" in config
    assert "daily" in config
    assert "rotate 14" in config
    assert "compress" in config
    assert "create 0640 financeapp adm" in config


def test_installer_is_root_only_and_ubuntu_24_04_idempotent() -> None:
    script = read("install.sh")
    assert "EUID" in script and "root" in script
    assert "VERSION_ID" in script and "24.04" in script
    for package in (
        "python3-venv",
        "python3-dev",
        "build-essential",
        "nginx",
        "tesseract-ocr",
        "tesseract-ocr-chi-sim",
    ):
        assert package in script
    assert "useradd" in script and "--system" in script
    assert "/opt/personal-finance/releases" in script
    assert "git rev-parse HEAD" in script
    assert "/opt/personal-finance/current" in script
    for directory in ("data", "backups", "uploads"):
        assert f"/var/lib/personal-finance/{directory}" in script
    assert "systemctl daemon-reload" in script
    assert "systemctl enable" in script
    assert "security group" in script.lower()
    assert "Tailscale" in script
    assert "add an inbound TCP 80 rule" not in script
    assert "docker" not in script.lower()
    assert "sshd_config" not in script


def test_installer_does_not_overwrite_secrets_or_run_trades() -> None:
    script = read("install.sh").lower()
    assert "finance.env" in script
    assert "chmod 600" not in script
    assert "buy" not in script
    assert "sell" not in script


def test_installer_publishes_a_clean_root_owned_git_release() -> None:
    script = read("install.sh")
    assert "RELEASE_SHA" in script
    assert "command -v git" in script
    assert "git archive" in script
    assert "--exclude=.env" in script
    assert "--exclude=data" in script
    assert "--exclude=.venv" in script
    assert 'cp -a "$SOURCE_DIR/.' not in script
    assert 'chown -R financeapp:financeapp "$STATE_ROOT" "$RELEASE_DIR"' not in script
    assert 'chown -R root:root "$RELEASE_DIR"' in script
    assert "FINANCE_SESSION_HTTPS_ONLY=true" in script
    assert "without TLS" not in script


def test_installer_relocks_existing_account_and_repairs_private_permissions() -> None:
    script = read("install.sh")
    assert "usermod --shell /usr/sbin/nologin" in script
    assert "--home \"$STATE_ROOT\"" in script
    assert "--lock financeapp" in script
    assert 'chmod 0600 "$ENV_FILE"' in script
    for directory in ("data", "backups", "uploads"):
        assert f'chmod 0750 "$STATE_ROOT/{directory}"' in script


def test_csp_allows_only_the_templates_controlled_inline_widths() -> None:
    config = read("nginx-finance.conf")
    assert "style-src 'self' 'unsafe-inline'" in config
