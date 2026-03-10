#!/usr/bin/env bash
# ============================================================
# Translation Proxy – Hotfix: certbot read-only filesystem
# Run as root on your server:
#   curl -fsSL <url>/fix_certbot_permissions.sh | sudo bash
#   OR copy this file and: sudo bash fix_certbot_permissions.sh
# ============================================================
set -euo pipefail

GREEN='\033[0;32m'; NC='\033[0m'
info() { echo -e "${GREEN}[FIX]${NC} $*"; }

APP_DIR="${APP_DIR:-/opt/translation-proxy}"

# ── 1. Sudoers: add certbot + nginx for www-data ──────────────────────────────
info "Updating sudoers..."
cat > /etc/sudoers.d/www-data-proxy << 'SUDOEOF'
# Translation proxy – allow app user to manage nginx and certbot
www-data ALL=(root) NOPASSWD: /usr/sbin/nginx
www-data ALL=(root) NOPASSWD: /usr/bin/nginx
www-data ALL=(root) NOPASSWD: /usr/bin/certbot
www-data ALL=(root) NOPASSWD: /usr/local/bin/certbot
SUDOEOF
chmod 440 /etc/sudoers.d/www-data-proxy
# Remove old file if present
rm -f /etc/sudoers.d/www-data-nginx
visudo -c && info "sudoers OK"

# ── 2. Ensure LE dirs exist and are root-owned (certbot via sudo writes there) ─
info "Ensuring Let's Encrypt directories exist..."
mkdir -p /etc/letsencrypt /var/lib/letsencrypt /var/log/letsencrypt
# certbot runs as root via sudo, so root owns these – that's correct

# ── 3. Patch systemd service to remove NoNewPrivileges ────────────────────────
SERVICE_FILE="/etc/systemd/system/translation-proxy.service"
if [[ -f "${SERVICE_FILE}" ]]; then
    info "Patching systemd service..."
    # Remove NoNewPrivileges line so sudo works inside the service
    sed -i '/^NoNewPrivileges/d' "${SERVICE_FILE}"
    # Replace ProtectSystem=strict with full (strict blocks /usr writes needed by sudo)
    sed -i 's/ProtectSystem=strict/ProtectSystem=full/' "${SERVICE_FILE}"
    # Add letsencrypt work/log dirs to ReadWritePaths if not already there
    if ! grep -q "var/lib/letsencrypt" "${SERVICE_FILE}"; then
        sed -i 's|ReadWritePaths=\(.*\)$|ReadWritePaths=\1 /var/lib/letsencrypt /var/log/letsencrypt|' "${SERVICE_FILE}"
    fi
    systemctl daemon-reload
    info "Service patched and daemon reloaded"
else
    info "Service file not found at ${SERVICE_FILE} – skipping (manual setup?)"
fi

# ── 4. Patch ssl_manager.py to use sudo ───────────────────────────────────────
SSL_MANAGER="${APP_DIR}/control_plane/app/services/ssl_manager.py"
if [[ -f "${SSL_MANAGER}" ]]; then
    info "Patching ssl_manager.py to use sudo..."
    # Inject sudo into _run() if not already there
    if ! grep -q '"sudo"' "${SSL_MANAGER}"; then
        python3 - << 'PYEOF'
import re, sys

path = "/opt/translation-proxy/control_plane/app/services/ssl_manager.py"
try:
    src = open(path).read()
except FileNotFoundError:
    import os; path = os.environ.get("SSL_MANAGER", path); src = open(path).read()

# Replace the _run function body to prepend sudo
old = '''def _run(cmd: list[str], timeout: int = 120) -> Tuple[int, str, str]:
    """Run a shell command and return (returncode, stdout, stderr)."""
    logger.info("Running: %s", " ".join(cmd))
    result = subprocess.run(
        cmd,'''

new = '''def _run(cmd: list[str], timeout: int = 120) -> Tuple[int, str, str]:
    """Run a command with sudo and return (returncode, stdout, stderr)."""
    full_cmd = ["sudo"] + cmd
    logger.info("Running: %s", " ".join(full_cmd))
    result = subprocess.run(
        full_cmd,'''

if old in src:
    src = src.replace(old, new)
    open(path, 'w').write(src)
    print("ssl_manager.py patched")
else:
    print("Pattern not found in ssl_manager.py – may already be patched")
PYEOF
    else
        info "ssl_manager.py already has sudo – no change needed"
    fi
fi

# ── 5. Add certbot dir settings to .env if missing ────────────────────────────
ENV_FILE="${APP_DIR}/.env"
if [[ -f "${ENV_FILE}" ]]; then
    info "Checking .env for certbot dir settings..."
    grep -q "CERTBOT_CONFIG_DIR" "${ENV_FILE}" || \
        echo "CERTBOT_CONFIG_DIR=/etc/letsencrypt" >> "${ENV_FILE}"
    grep -q "CERTBOT_WORK_DIR" "${ENV_FILE}" || \
        echo "CERTBOT_WORK_DIR=/var/lib/letsencrypt" >> "${ENV_FILE}"
    grep -q "CERTBOT_LOGS_DIR" "${ENV_FILE}" || \
        echo "CERTBOT_LOGS_DIR=/var/log/letsencrypt" >> "${ENV_FILE}"
    info ".env updated"
fi

# ── 6. Restart the service ────────────────────────────────────────────────────
if systemctl is-active --quiet translation-proxy 2>/dev/null; then
    info "Restarting translation-proxy service..."
    systemctl restart translation-proxy
    sleep 2
    systemctl is-active --quiet translation-proxy && \
        info "Service restarted successfully" || \
        echo "WARNING: Service may not have started – check: journalctl -u translation-proxy -n 30"
fi

# ── 7. Quick test ─────────────────────────────────────────────────────────────
info "Testing sudo certbot access..."
sudo -u www-data sudo certbot --version && \
    info "www-data can run certbot via sudo ✅" || \
    echo "WARNING: www-data cannot run certbot via sudo – check /etc/sudoers.d/www-data-proxy"

echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║  Hotfix applied! Retry provision-ssl now.    ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════╝${NC}"
echo ""
echo "  Retry with:"
echo '  curl -s -X POST "${BASE}/domains/${CUSTOMER_DOMAIN}/provision-ssl" \'
echo '    -H "X-API-Key: $API_KEY" | python3 -m json.tool'
echo ""
