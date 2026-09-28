# Tailscale Private Access Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace routine SSH port-forward access with a private Tailscale HTTPS endpoint while keeping the finance application and Nginx unavailable from the public internet.

**Architecture:** Tailscale Serve terminates HTTPS inside the user's Tailnet and proxies to Nginx on `127.0.0.1:80`; Nginx continues to proxy to Uvicorn on `127.0.0.1:8000`. Repository deployment artifacts enforce loopback-only Nginx and HTTPS-only session cookies, while the existing restricted public-key SSH path remains available for maintenance and rollback.

**Tech Stack:** Ubuntu 24.04, Tailscale/tailscaled, Tailscale Serve, Nginx, systemd, FastAPI session cookies, pytest, Alibaba Cloud security groups

---

## File Map

- Modify `tests/deploy/test_artifacts.py`: enforce loopback-only Nginx, secure-cookie bootstrap, and removal of public TCP 80 guidance.
- Modify `deploy/nginx-finance.conf`: bind Nginx only to IPv4 and IPv6 loopback.
- Modify `deploy/install.sh`: create secure-cookie configuration by default and print Tailscale-oriented post-install guidance.
- Modify `docs/operations.md`: document Tailscale installation, authorization, Serve configuration, verification, recovery, and client use.
- Modify `/etc/nginx/sites-available/personal-finance` on the ECS: apply the verified loopback-only listener.
- Modify `/etc/personal-finance/finance.env` on the ECS: set `FINANCE_SESSION_HTTPS_ONLY=true` without exposing other environment values.

### Task 1: Lock The Private-Access Contract With Failing Tests

**Files:**
- Modify: `tests/deploy/test_artifacts.py`
- Test: `tests/deploy/test_artifacts.py`

- [ ] **Step 1: Replace the permissive Nginx assertion with loopback-only assertions**

Update `test_nginx_proxies_only_loopback_and_blocks_sensitive_files` so its listener checks are:

```python
assert "listen 127.0.0.1:80 default_server;" in config
assert "listen [::1]:80 default_server;" in config
assert not re.search(r"(?m)^\s*listen\s+80(?:\s|;)", config)
assert not re.search(r"(?m)^\s*listen\s+\[::\]:80(?:\s|;)", config)
```

- [ ] **Step 2: Change installer expectations to HTTPS-only private access**

In `test_installer_publishes_a_clean_root_owned_git_release`, replace the HTTP bootstrap assertions with:

```python
assert "FINANCE_SESSION_HTTPS_ONLY=true" in script
assert "without TLS" not in script
```

In `test_installer_is_root_only_and_ubuntu_24_04_idempotent`, replace the `TCP 80` assertion with:

```python
assert "Tailscale" in script
assert "add an inbound TCP 80 rule" not in script
```

- [ ] **Step 3: Run the focused tests and verify they fail for the expected old configuration**

Run:

```powershell
python -m pytest tests/deploy/test_artifacts.py -q
```

Expected: failures report the existing wildcard Nginx listeners, `FINANCE_SESSION_HTTPS_ONLY=false`, and the old TCP 80 message.

### Task 2: Make Deployment Artifacts Private By Default

**Files:**
- Modify: `deploy/nginx-finance.conf`
- Modify: `deploy/install.sh`
- Test: `tests/deploy/test_artifacts.py`

- [ ] **Step 1: Bind Nginx only to loopback**

Replace the first two directives in `deploy/nginx-finance.conf` with:

```nginx
listen 127.0.0.1:80 default_server;
listen [::1]:80 default_server;
```

- [ ] **Step 2: Make new installations require HTTPS session cookies**

In the environment-file creation block in `deploy/install.sh`, use:

```bash
'FINANCE_SESSION_HTTPS_ONLY=true' \
'# Keep true: the supported remote entry point is Tailnet-only HTTPS via Tailscale Serve.' > "$ENV_FILE"
```

- [ ] **Step 3: Replace the public-port post-install message**

Use this final installer message:

```bash
cat <<'MESSAGE'
Installation completed. The application is prepared but is not started until its
database migration and administrator are initialized. Nginx listens only on the
server loopback interface. Configure Tailnet-only HTTPS with Tailscale Serve before
logging in; do not add public TCP 80 or 443 security-group rules. No cloud firewall
rule was changed by this script.
MESSAGE
```

- [ ] **Step 4: Run the focused deployment tests**

Run:

```powershell
python -m pytest tests/deploy/test_artifacts.py -q
```

Expected: all tests in the file pass.

- [ ] **Step 5: Commit the artifact changes**

```powershell
git add tests/deploy/test_artifacts.py deploy/nginx-finance.conf deploy/install.sh
git commit -m "security: default deployment to private HTTPS access"
```

### Task 3: Document Installation, Recovery, And Client Access

**Files:**
- Modify: `docs/operations.md`
- Test: `tests/test_security_regressions.py`

- [ ] **Step 1: Add a Tailscale private-access runbook section**

Document these commands without including an auth key or Tailnet name:

```bash
curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/noble.noarmor.gpg \
  -o /usr/share/keyrings/tailscale-archive-keyring.gpg
curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/noble.tailscale-keyring.list \
  -o /etc/apt/sources.list.d/tailscale.list
apt-get update
apt-get install -y tailscale
systemctl enable --now tailscaled
tailscale up --ssh=false --accept-routes=false --accept-dns=true
tailscale serve --bg --https=443 http://127.0.0.1:80
tailscale status
tailscale serve status
tailscale funnel status
```

State that the login URL printed by `tailscale up` is opened only by the owner, Funnel must remain disabled, and the `.ts.net` HTTPS address must be tested before changing listeners or cloud rules.

- [ ] **Step 2: Add verification and recovery commands**

Include:

```bash
nginx -t
ss -lntp | grep -E ':(80|8000)\b'
curl --fail --silent http://127.0.0.1/health
systemctl is-active tailscaled nginx finance-app finance-daily.timer finance-backup.timer
```

Document recovery without deleting state:

```bash
tailscale serve reset
cp /etc/nginx/sites-available/personal-finance.pre-tailscale \
  /etc/nginx/sites-available/personal-finance
nginx -t && systemctl reload nginx
```

- [ ] **Step 3: Extend the security-documentation regression test**

Add `"Tailscale"`, `"Funnel"`, and `"tailscale serve"` to the required markers for `docs/operations.md` in `test_operations_and_release_artifacts_exist_with_security_guidance`.

- [ ] **Step 4: Run the documentation security test**

Run:

```powershell
python -m pytest tests/test_security_regressions.py -q
```

Expected: all tests in the file pass.

- [ ] **Step 5: Commit the runbook**

```powershell
git add docs/operations.md tests/test_security_regressions.py
git commit -m "docs: add Tailscale private access runbook"
```

### Task 4: Run The Repository Release Gate

**Files:**
- Verify only; do not modify application data or secrets.

- [ ] **Step 1: Check formatting and the focused suite**

Run:

```powershell
git diff --check HEAD~2
python -m pytest tests/deploy/test_artifacts.py tests/test_security_regressions.py -q
```

Expected: no whitespace errors and all focused tests pass.

- [ ] **Step 2: Run the complete Python test suite**

Run:

```powershell
python -m pytest -q
```

Expected: zero failures.

- [ ] **Step 3: Confirm only the pre-existing README work remains uncommitted**

Run:

```powershell
git status --short
```

Expected: `README.md` may remain modified from earlier user work; no Tailscale implementation file remains uncommitted.

### Task 5: Capture A Server Preflight And Recovery Point

**Files:**
- Read: `/etc/nginx/sites-available/personal-finance`
- Read: `/etc/personal-finance/finance.env` only through key-name/redacted checks
- Create: `/etc/nginx/sites-available/personal-finance.pre-tailscale`

- [ ] **Step 1: Verify SSH and current health without printing secrets**

Run from the authorized Windows computer:

```powershell
ssh -o BatchMode=yes -o IdentitiesOnly=yes `
  -i "$env:USERPROFILE\.ssh\aliyun_finance_ed25519" `
  root@121.41.225.151 `
  "systemctl is-active nginx finance-app finance-daily.timer finance-backup.timer; curl --fail --silent http://127.0.0.1/health; ss -lntp | grep -E ':(22|80|8000)\\b'"
```

Expected: all existing units are active, health is OK, SSH and Nginx are listening, and Uvicorn listens only on loopback.

- [ ] **Step 2: Verify a recent database backup without changing the live database**

Run:

```bash
latest_backup="$(find /var/lib/personal-finance/backups -maxdepth 1 \
  -type f -name '*.sqlite3' -printf '%T@ %p\n' | sort -n | tail -1 | cut -d' ' -f2-)"
test -n "$latest_backup"
cd /opt/personal-finance/current
sudo -u financeapp env \
  FINANCE_DATABASE_URL=sqlite:////var/lib/personal-finance/data/finance.db \
  .venv/bin/finance restore-check "$latest_backup"
```

Expected: SQLite integrity and schema checks pass without replacing the live database.

- [ ] **Step 3: Back up the Nginx site configuration**

```bash
install -o root -g root -m 0644 \
  /etc/nginx/sites-available/personal-finance \
  /etc/nginx/sites-available/personal-finance.pre-tailscale
```

### Task 6: Install Tailscale And Complete Owner Authorization

**Files:**
- Create through packages: `/etc/apt/sources.list.d/tailscale.list`
- Create through packages: `/usr/share/keyrings/tailscale-archive-keyring.gpg`
- Managed by Tailscale: `/var/lib/tailscale/`

- [ ] **Step 1: Add the official Ubuntu 24.04 repository and install Tailscale**

Run the exact official-repository commands recorded in Task 3, then verify:

```bash
tailscale version
systemctl is-enabled tailscaled
systemctl is-active tailscaled
```

Expected: a version is printed and both systemd checks succeed.

- [ ] **Step 2: Start device authorization without enabling Tailscale SSH**

```bash
tailscale up --ssh=false --accept-routes=false --accept-dns=true
```

Expected: the command prints a `https://login.tailscale.com/...` authorization URL.

- [ ] **Step 3: Pause for the owner to authorize the ECS**

The owner opens the URL, signs into the intended personal Tailnet, confirms the ECS, and reports completion. Do not proceed to Nginx or security-group changes before this confirmation.

- [ ] **Step 4: Verify Tailnet membership**

```bash
tailscale status
tailscale ip -4
```

Expected: the ECS is online and has a `100.x.y.z` Tailnet address.

### Task 7: Enable And Verify Tailnet-Only HTTPS

**Files:**
- Managed by Tailscale Serve; no repository secret files.

- [ ] **Step 1: Start persistent HTTPS proxying**

```bash
tailscale serve --bg --https=443 http://127.0.0.1:80
```

If Tailscale requests HTTPS enablement, open only the provided Tailscale admin URL, enable HTTPS certificates for this Tailnet, and rerun the command.

- [ ] **Step 2: Confirm Serve is private and Funnel is disabled**

```bash
tailscale serve status
tailscale funnel status
```

Expected: Serve reports a Tailnet-only HTTPS URL and Funnel reports no active public service.

- [ ] **Step 3: Test from an authorized client before changing Nginx**

Install and sign into Tailscale on the user's computer, open the printed `.ts.net` URL, log in to the finance platform, and verify the dashboard and settings pages.

### Task 8: Enforce Loopback Nginx And Secure Cookies On Production

**Files:**
- Modify: `/etc/nginx/sites-available/personal-finance`
- Modify: `/etc/personal-finance/finance.env`

- [ ] **Step 1: Deploy the reviewed Nginx listener configuration**

From the authorized Windows computer, copy the repository's tested configuration to a temporary server path:

```powershell
scp -o IdentitiesOnly=yes `
  -i "$env:USERPROFILE\.ssh\aliyun_finance_ed25519" `
  deploy/nginx-finance.conf `
  root@121.41.225.151:/tmp/nginx-finance.conf
```

On the server, install it, validate the complete Nginx configuration, and reload only after validation succeeds:

```bash
install -o root -g root -m 0644 /tmp/nginx-finance.conf \
  /etc/nginx/sites-available/personal-finance
nginx -t
systemctl reload nginx
```

Expected: syntax succeeds and reload completes without error.

- [ ] **Step 2: Enable HTTPS-only session cookies without printing the environment**

Update only the existing key:

```bash
sed -i 's/^FINANCE_SESSION_HTTPS_ONLY=.*/FINANCE_SESSION_HTTPS_ONLY=true/' \
  /etc/personal-finance/finance.env
systemctl restart finance-app
```

Verify the key exists exactly once using a redacted Boolean check, not `cat` on the environment file.

- [ ] **Step 3: Verify listeners, services, HTTP health, and private HTTPS**

Run:

```bash
nginx -t
ss -lntp | grep -E ':(80|8000)\b'
curl --fail --silent http://127.0.0.1/health
systemctl is-active tailscaled nginx finance-app finance-daily.timer finance-backup.timer
tailscale serve status
tailscale funnel status
```

Expected: Nginx is loopback-only on port 80, Uvicorn is loopback-only on 8000, all services are active, health is OK, Serve is active, and Funnel is disabled.

- [ ] **Step 4: Verify browser session security**

From an authorized Tailscale client, sign out and sign back in through the HTTPS address. Inspect the login response or browser storage and confirm the session Cookie has `Secure`, `HttpOnly`, and the configured `SameSite` attribute.

### Task 9: Close Public Website Reachability And Test Persistence

**Files:**
- Alibaba Cloud security-group state; no application data changes.

- [ ] **Step 1: Confirm public website ports are closed**

From a device not connected to Tailscale:

```powershell
Test-NetConnection 121.41.225.151 -Port 80
Test-NetConnection 121.41.225.151 -Port 443
Test-NetConnection 121.41.225.151 -Port 8000
```

Expected: all three `TcpTestSucceeded` values are `False`. If any succeeds, remove only that public inbound website rule in the Alibaba Cloud security group. Keep the existing restricted TCP 22 rule.

- [ ] **Step 2: Reboot only after all live checks pass**

Before rebooting, confirm the latest database backup and the Nginx recovery copy exist. Reboot the ECS, wait for SSH to return, then rerun all commands from Task 8 Step 3.

Expected: Tailscale, Serve configuration, Nginx, the application, daily timer, and backup timer recover automatically.

- [ ] **Step 3: Perform the final client check**

Open the fixed `.ts.net` HTTPS address on the authorized computer and phone. Confirm both can log in and that disconnecting Tailscale makes the site unreachable.

- [ ] **Step 4: Record non-secret deployment verification**

Append the deployment date, service status, listener boundaries, HTTPS URL hostname only if the owner approves recording it, and the successful checks to a new `docs/deployment-verification-2026-09-28-tailscale.md`. Never record auth URLs, device keys, Tailnet credentials, environment values, or cookies.

