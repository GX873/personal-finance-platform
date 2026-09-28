from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_private_state_paths_are_not_public(client) -> None:
    """The ASGI app must not expose deployment state files as web resources."""
    for path in ("/.env", "/data/finance.db", "/backups/latest.sqlite3"):
        response = client.get(path)
        assert response.status_code in {403, 404}
        assert "secret" not in response.text.lower()


def test_nginx_denies_private_state_paths() -> None:
    config = (PROJECT_ROOT / "deploy" / "nginx-finance.conf").read_text(
        encoding="utf-8"
    )
    for path in ("location ~ /", "location ^~ /data/", "location ^~ /backups/"):
        assert path in config
    assert "deny all;" in config


def test_operations_and_release_artifacts_exist_with_security_guidance() -> None:
    required = {
        "README.md": ("公网 HTTP", "TLS"),
        "docs/operations.md": (
            "restore-check",
            "rollback",
            "timer",
            "Tailscale",
            "Funnel",
            "tailscale serve",
        ),
        "docs/data-import.md": ("CSV", "XLSX", "OCR", "confirmation"),
        "scripts/release-check.sh": ("pytest", "mypy", "Ruff", "mktemp"),
    }
    for relative_path, markers in required.items():
        content = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
        for marker in markers:
            assert marker.lower() in content.lower(), (relative_path, marker)


def test_release_check_scans_secrets_without_printing_values() -> None:
    script = (PROJECT_ROOT / "scripts" / "release-check.sh").read_text(
        encoding="utf-8"
    )
    assert "FINANCE_SMTP_AUTHORIZATION_CODE" in script
    assert "FINANCE_PUSHPLUS_TOKEN" in script
    assert "FINANCE_SERVERCHAN_SENDKEY" in script
    assert "FINANCE_WECOM_WEBHOOK_URL" in script
    assert "git grep" in script or "rg" in script
    assert 'COVERAGE_FILE="$TMP_DIR/.coverage"' in script
    assert "trap" in script
