#!/usr/bin/env bash
# ============================================================
# Translation Proxy – Fix: ACME challenge + auto-renewal
#
# Fixes:
#   1. Opens port 80 to ALL IPs (required for Let's Encrypt)
#   2. Keeps port 443 locked to Cloudflare only
#   3. Installs systemd certbot renewal timer
#   4. Verifies nginx serves the ACME webroot correctly
#   5. Deploys updated code (cloudflare_manager, ssl_manager, main.py)
#
# Run as root:  sudo bash scripts/fix_acme_and_renewal.sh
# ============================================================
set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[FIX]${NC}   $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

APP_DIR="/opt/translation-proxy"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "${SCRIPT_DIR}")"

# ── 1. Deploy updated code ────────────────────────────────────────────────────
info "Deploying updated code..."
rsync -a --delete "${PROJECT_DIR}/control_plane/" "${APP_DIR}/control_plane/"
chown -R www-data:www-data "${APP_DIR}"
ok "Code deployed"

# ── 2. Fix UFW: open port 80, lock 443 to CF only ────────────────────────────
info "Fixing UFW firewall rules..."

# Remove ALL existing rules for 80 and 443
for port in 80 443; do
    while ufw status numbered 2>/dev/null | grep -q " ${port}[/ ]"; do
        RULE_NUM=$(ufw status numbered | grep " ${port}[/ ]" | \
                   head -1 | sed 's/\[ *\([0-9]*\)\].*/\1/')
        [ -n "$RULE_NUM" ] && ufw --force delete "${RULE_NUM}" 2>/dev/null || break
    done
done

# Port 80: fully open (Let's Encrypt ACME challenge must reach this)
ufw allow 80/tcp comment "http-acme-open"
ok "Port 80 opened to all (required for Let's Encrypt ACME challenge)"

# Port 443: Cloudflare IPs only
# Load from cache if available, otherwise use fallback list
CF_IPS_FILE="/etc/nginx/cloudflare-ips.json"
if [[ -f "${CF_IPS_FILE}" ]]; then
    info "Loading Cloudflare IPs from cache..."
    mapfile -t CF_IPV4 < <(python3 -c "
import json
d = json.load(open('${CF_IPS_FILE}'))
print('\n'.join(d.get('ipv4', [])))
")
else
    info "No CF IP cache found, using fallback list..."
    CF_IPV4=(
        "173.245.48.0/20" "103.21.244.0/22" "103.22.200.0/22" "103.31.4.0/22"
        "141.101.64.0/18" "108.162.192.0/18" "190.93.240.0/20" "188.114.96.0/20"
        "197.234.240.0/22" "198.41.128.0/17" "162.158.0.0/15"  "104.16.0.0/13"
        "104.24.0.0/14"   "172.64.0.0/13"   "131.0.72.0/22"
    )
fi

for cidr in "${CF_IPV4[@]}"; do
    ufw allow proto tcp from "${cidr}" to any port 443 comment "cloudflare-https" 2>/dev/null || true
done

# Deny all other direct connections to 443
ufw deny 443/tcp

ufw reload
ok "Port 443 locked to Cloudflare IPs only"

info "Current UFW status:"
ufw status numbered | grep -E "80|443" | head -20

# ── 3. Ensure nginx ACME webroot is properly configured ───────────────────────
info "Configuring ACME webroot..."
WEBROOT="/var/www/acme-challenge"
mkdir -p "${WEBROOT}/.well-known/acme-challenge"
chown -R www-data:www-data "${WEBROOT}"

# Check that nginx has the default HTTP config to serve the webroot
DEFAULT_HTTP="/etc/nginx/sites-available/000-default-acme.conf"
if [[ ! -f "${DEFAULT_HTTP}" ]]; then
    info "Creating default ACME webroot server block..."
    cat > "${DEFAULT_HTTP}" << 'NGEOF'
# Catch-all HTTP server – serves ACME challenges for ALL domains.
# This is hit when a domain's own HTTP config doesn't exist yet.
server {
    listen 80 default_server;
    server_name _;

    location /.well-known/acme-challenge/ {
        root /var/www/acme-challenge;
        try_files $uri =404;
    }

    location / {
        return 444;
    }
}
NGEOF
    # Only link if default_server isn't already claimed by another config
    if ! grep -r "default_server" /etc/nginx/sites-enabled/ 2>/dev/null | grep -v "default_server ssl"; then
        ln -sf "${DEFAULT_HTTP}" "/etc/nginx/sites-enabled/000-default-acme.conf"
    fi
fi

# ── 4. Check existing nginx.conf doesn't have conflicting default_server ──────
# Remove the built-in default site if present
rm -f /etc/nginx/sites-enabled/default

# ── 5. Test and reload nginx ──────────────────────────────────────────────────
info "Testing nginx configuration..."
nginx -t || error "nginx config test failed – check errors above"
nginx -s reload
ok "Nginx reloaded"

# ── 6. Install systemd certbot renewal timer ──────────────────────────────────
info "Installing certbot renewal systemd timer..."

cp "${PROJECT_DIR}/scripts/systemd/certbot-proxy-renew.service" \
   /etc/systemd/system/certbot-proxy-renew.service
cp "${PROJECT_DIR}/scripts/systemd/certbot-proxy-renew.timer" \
   /etc/systemd/system/certbot-proxy-renew.timer

systemctl daemon-reload
systemctl enable  certbot-proxy-renew.timer
systemctl start   certbot-proxy-renew.timer

# Disable the old cron-based renewal to avoid conflicts
systemctl disable --now certbot.timer 2>/dev/null || true
# Remove the old cron entry if it exists
sed -i '/renew_certs.sh/d' /etc/cron.d/certbot-renew 2>/dev/null || true

ok "Certbot renewal timer installed:"
systemctl list-timers certbot-proxy-renew.timer --no-pager

# ── 7. Restart the control plane ─────────────────────────────────────────────
info "Restarting translation-proxy service..."
systemctl restart translation-proxy
sleep 2
systemctl is-active --quiet translation-proxy && \
    ok "translation-proxy is running" || \
    error "Service failed – run: journalctl -u translation-proxy -n 50"

# ── 8. Verify port 80 serves ACME webroot ─────────────────────────────────────
info "Verifying ACME webroot..."
TEST_TOKEN="setup-verify-$$"
TEST_FILE="${WEBROOT}/.well-known/acme-challenge/${TEST_TOKEN}"
echo "ok" > "${TEST_FILE}"
chown www-data:www-data "${TEST_FILE}"

sleep 1
RESULT=$(curl -sf "http://localhost/.well-known/acme-challenge/${TEST_TOKEN}" 2>/dev/null || echo "FAILED")
rm -f "${TEST_FILE}"

if [[ "${RESULT}" == "ok" ]]; then
    ok "ACME webroot is being served correctly on port 80 ✅"
else
    warn "ACME webroot test returned: '${RESULT}'"
    warn "Check: nginx -t && curl -v http://localhost/.well-known/acme-challenge/test"
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║  Fix applied! Now retry provision-ssl:                       ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo '  curl -s -X POST "$BASE/domains/$CUSTOMER_DOMAIN/provision-ssl" \'
echo '    -H "X-API-Key: $API_KEY" | python3 -m json.tool'
echo ""
echo "  If it still fails, check the actual certbot error:"
echo "  sudo tail -50 /var/log/letsencrypt/letsencrypt.log"
echo ""
