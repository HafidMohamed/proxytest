#!/usr/bin/env bash
# ============================================================
# Translation Proxy – Quick deploy / update script
# Copies updated code to /opt/translation-proxy and restarts.
#
# Usage (run on the SERVER, from inside the project folder):
#   sudo bash scripts/deploy_update.sh
#
# Or from your LOCAL machine:
#   scp -r control_plane root@SERVER:/opt/translation-proxy/
#   ssh root@SERVER "bash /opt/translation-proxy/scripts/deploy_update.sh"
# ============================================================
set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[DEPLOY]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}   $*"; }
error() { echo -e "${RED}[ERROR]${NC}  $*"; exit 1; }

APP_DIR="/opt/translation-proxy"
VENV="${APP_DIR}/venv"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "${SCRIPT_DIR}")"

info "Project dir : ${PROJECT_DIR}"
info "App dir     : ${APP_DIR}"

# ── 1. Copy updated application code ──────────────────────────────────────────
info "Copying application code..."
mkdir -p "${APP_DIR}"
rsync -a --delete \
    "${PROJECT_DIR}/control_plane/" \
    "${APP_DIR}/control_plane/"

# ── 2. Copy updated nginx.conf ────────────────────────────────────────────────
info "Updating nginx.conf..."
cp "${PROJECT_DIR}/nginx/nginx.conf" /etc/nginx/nginx.conf

# ── 3. Copy scripts ───────────────────────────────────────────────────────────
info "Updating scripts..."
cp "${PROJECT_DIR}/scripts/"*.sh "${APP_DIR}/scripts/" 2>/dev/null || \
    rsync -a "${PROJECT_DIR}/scripts/" "${APP_DIR}/scripts/"
chmod +x "${APP_DIR}/scripts/"*.sh

# ── 4. Install / update Python dependencies ───────────────────────────────────
info "Installing Python dependencies..."
"${VENV}/bin/pip" install --upgrade pip -q
"${VENV}/bin/pip" install -r "${APP_DIR}/control_plane/requirements.txt" -q

# ── 5. Fix ownership ──────────────────────────────────────────────────────────
chown -R www-data:www-data "${APP_DIR}"

# ── 6. Test nginx config ──────────────────────────────────────────────────────
info "Testing nginx configuration..."
nginx -t || error "nginx config test failed – fix errors before restarting"

# ── 7. Restart app service ────────────────────────────────────────────────────
info "Restarting translation-proxy service..."
systemctl restart translation-proxy
sleep 2

if systemctl is-active --quiet translation-proxy; then
    info "Service is running ✅"
else
    error "Service failed to start. Check: journalctl -u translation-proxy -n 50"
fi

# ── 8. Reload nginx ───────────────────────────────────────────────────────────
info "Reloading nginx..."
nginx -s reload
info "Nginx reloaded ✅"

# ── 9. Verify health ──────────────────────────────────────────────────────────
sleep 1
HEALTH=$(curl -sf http://localhost:8000/health 2>/dev/null || echo "FAILED")
if echo "${HEALTH}" | grep -q '"ok"'; then
    info "Health check passed ✅  → ${HEALTH}"
else
    warn "Health check returned: ${HEALTH}"
    warn "Check logs: journalctl -u translation-proxy -n 30"
fi

# ── 10. Check cloudflare endpoint ─────────────────────────────────────────────
CF_STATUS=$(curl -sf http://localhost:8000/cloudflare/status 2>/dev/null || echo "FAILED")
if echo "${CF_STATUS}" | grep -q "ipv4_count"; then
    info "Cloudflare endpoint OK ✅"
else
    warn "Cloudflare endpoint issue: ${CF_STATUS}"
fi

echo ""
echo -e "${GREEN}╔══════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║  Deploy complete!                        ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════╝${NC}"
echo ""
echo "  API docs : http://$(curl -sf https://ipinfo.io/ip 2>/dev/null || hostname -I | awk '{print $1}'):8000/docs"
echo ""
echo "  Next: refresh Cloudflare IPs:"
echo "  curl -s -X POST http://localhost:8000/cloudflare/refresh | python3 -m json.tool"
echo ""
