#!/usr/bin/env bash
set -Eeuo pipefail

# Run the release gate from the repository root. Every generated artifact lives
# in one private temporary directory and the trap removes only that directory.
ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PYTHON="${PYTHON:-python3}"
TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/personal-finance-release.XXXXXX")"
cleanup() {
    rm -rf -- "$TMP_DIR"
}
trap cleanup EXIT INT TERM

export PYTHONPATH="$ROOT_DIR${PYTHONPATH:+:$PYTHONPATH}"
export FINANCE_DATABASE_URL="sqlite:///$TMP_DIR/pytest.sqlite3"
export COVERAGE_FILE="$TMP_DIR/.coverage"

step() {
    printf '\n==> %s\n' "$*"
    "$@"
}

step "$PYTHON" -m ruff check finance_app tests
step "$PYTHON" -m mypy finance_app
step "$PYTHON" -m pytest -q --cov=finance_app --cov-report=term-missing --cov-fail-under=85

printf '\n==> temporary Alembic upgrade\n'
FINANCE_DATABASE_URL="sqlite:///$TMP_DIR/migration.sqlite3" \
    "$PYTHON" -m alembic upgrade head

printf '\n==> import smoke\n'
FINANCE_DATABASE_URL="sqlite:///$TMP_DIR/import-smoke.sqlite3" \
    "$PYTHON" -c 'from finance_app.app import create_app; assert create_app().title == "Personal Finance"'

printf '\n==> shell syntax\n'
shopt -s nullglob
shell_files=(deploy/*.sh scripts/*.sh)
if ((${#shell_files[@]} == 0)); then
    printf 'No shell scripts found.\n' >&2
    exit 1
fi
for shell_file in "${shell_files[@]}"; do
    bash -n "$shell_file"
done

printf '\n==> secret-pattern scan\n'
secret_report="$TMP_DIR/secret-scan.txt"
# Ignore tests and documentation because they contain deliberately fake token
# names/examples. Real credentials must never be committed to application code.
# The scan covers FINANCE_SMTP_AUTHORIZATION_CODE, FINANCE_PUSHPLUS_TOKEN,
# FINANCE_SERVERCHAN_SENDKEY, and FINANCE_WECOM_WEBHOOK_URL assignments.
if rg -n --hidden \
    --glob '!.git/**' \
    --glob '!tests/**' \
    --glob '!docs/**' \
    --glob '!scripts/release-check.sh' \
    'FINANCE_(SMTP_AUTHORIZATION_CODE|PUSHPLUS_TOKEN|SERVERCHAN_SENDKEY|WECOM_WEBHOOK_URL)=[^<[:space:]]{8,}|-----BEGIN (RSA|OPENSSH|EC|DSA) PRIVATE KEY-----|\bAKIA[0-9A-Z]{16}\b|\bgh[pousr]_[A-Za-z0-9]{20,}\b' \
    . >"$secret_report"; then
    printf 'Potential credential material found:\n' >&2
    sed -E 's/(=).*/\1<redacted>/' "$secret_report" >&2
    exit 1
fi

printf '\nRelease checks passed.\n'
