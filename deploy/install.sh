#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    echo "This installer must run as root." >&2
    exit 1
fi

if [[ ! -r /etc/os-release ]]; then
    echo "Cannot identify the operating system." >&2
    exit 1
fi
# shellcheck disable=SC1091
. /etc/os-release
if [[ "${ID:-}" != "ubuntu" || "${VERSION_ID:-}" != "24.04" ]]; then
    echo "Ubuntu 24.04 is required." >&2
    exit 1
fi

SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
# git rev-parse HEAD determines the immutable release directory name.
GIT_SHA="$(git -C "$SOURCE_DIR" rev-parse HEAD)"
RELEASE_ROOT=/opt/personal-finance/releases
RELEASE_DIR="$RELEASE_ROOT/$GIT_SHA"
CURRENT_LINK=/opt/personal-finance/current
STATE_ROOT=/var/lib/personal-finance
ENV_ROOT=/etc/personal-finance
ENV_FILE="$ENV_ROOT/finance.env"
# Persistent paths: /var/lib/personal-finance/data, /var/lib/personal-finance/backups,
# and /var/lib/personal-finance/uploads.

apt-get update
apt-get install -y --no-install-recommends \
    python3-venv python3-dev build-essential nginx \
    tesseract-ocr tesseract-ocr-chi-sim

if ! id financeapp >/dev/null 2>&1; then
    useradd --system --user-group --home-dir "$STATE_ROOT" \
        --shell /usr/sbin/nologin financeapp
fi
usermod --lock financeapp

install -d -m 0755 "$RELEASE_ROOT" "$ENV_ROOT" \
    "$STATE_ROOT/data" "$STATE_ROOT/backups" "$STATE_ROOT/uploads" \
    /var/log/personal-finance /etc/systemd/system

if [[ ! -e "$RELEASE_DIR" ]]; then
    install -d -m 0755 "$RELEASE_DIR"
    cp -a "$SOURCE_DIR/." "$RELEASE_DIR/"
fi
python3 -m venv "$RELEASE_DIR/.venv"
"$RELEASE_DIR/.venv/bin/python" -m pip install --upgrade pip
"$RELEASE_DIR/.venv/bin/python" -m pip install "$RELEASE_DIR"
ln -sfn "$RELEASE_DIR" "$CURRENT_LINK"

if [[ ! -e "$ENV_FILE" ]]; then
    install -o financeapp -g financeapp -m 600 /dev/null "$ENV_FILE"
    generated_secret="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
    printf '%s\n' \
        'FINANCE_ENVIRONMENT=production' \
        'FINANCE_DATABASE_URL=sqlite:////var/lib/personal-finance/data/finance.db' \
        "FINANCE_SECRET_KEY=$generated_secret" \
        'FINANCE_SESSION_HTTPS_ONLY=true' > "$ENV_FILE"
fi
chown financeapp:financeapp "$ENV_FILE"
chown -R financeapp:financeapp "$STATE_ROOT" "$RELEASE_DIR"
chown root:adm /var/log/personal-finance
chmod 0750 /var/log/personal-finance

for unit in finance-app.service finance-daily.service finance-daily.timer \
    finance-backup.service finance-backup.timer; do
    install -o root -g root -m 0644 "$SOURCE_DIR/deploy/$unit" \
        "/etc/systemd/system/$unit"
done
install -o root -g root -m 0644 "$SOURCE_DIR/deploy/nginx-finance.conf" \
    /etc/nginx/sites-available/personal-finance
install -o root -g root -m 0644 "$SOURCE_DIR/deploy/logrotate-finance" \
    /etc/logrotate.d/personal-finance
ln -sfn /etc/nginx/sites-available/personal-finance \
    /etc/nginx/sites-enabled/personal-finance
if [[ -L /etc/nginx/sites-enabled/default ]]; then
    rm -f /etc/nginx/sites-enabled/default
fi

nginx -t
systemctl daemon-reload
systemctl enable finance-app.service finance-daily.timer finance-backup.timer
systemctl enable nginx
systemctl enable --now finance-daily.timer finance-backup.timer
systemctl reload-or-restart nginx

cat <<'MESSAGE'
Installation completed. The application is prepared but is not started until its
database migration and administrator are initialized. The remaining manual Alibaba
Cloud security group action is: add an inbound TCP 80 rule to this ECS instance
(source restricted to the required clients where possible). No cloud firewall rule
was changed by this script.
MESSAGE
