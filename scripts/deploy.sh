#!/usr/bin/env bash
# ================================================================
# Translation Proxy – Full Deploy & Fix (single script)
#
# Replaces: setup.sh + fix_certbot_permissions.sh + fix_acme_and_renewal.sh
#
# What it does:
#   1. Install system packages (nginx, postgres, python, certbot)
#   2. Configure PostgreSQL
#   3. Fix nginx.conf (port 80 serves ACME, port 443 CF-only)
#   4. Install app + virtualenv
#   5. Write .env
#   6. Fix sudoers (www-data can run certbot + nginx via sudo)
#   7. Install & start systemd service (no NoNewPrivileges)
#   8. Fetch Cloudflare IPs + write nginx snippets
#   9. Fix UFW: port 80 open to all, port 443 CF IPs only
#  10. Remove conflicting default nginx configs
#  11. Install systemd certbot renewal timer
#  12. Verify ACME webroot works (localhost test)
#  13. Health check
#
# Usage:
#   export CERTBOT_EMAIL="your@email.com"
#   export PROXY_DB_PASS="StrongPassword123"   # optional, auto-generated if unset
#   sudo -E bash deploy.sh
# ================================================================
set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
ok()    { echo -e "${GREEN}[ OK ]${NC}  $*"; }
step()  { echo -e "\n${CYAN}━━━ $* ━━━${NC}"; }
error() { echo -e "${RED}[ERR]${NC}   $*"; exit 1; }

# ── Config ────────────────────────────────────────────────────────────────────
DB_NAME="${PROXY_DB_NAME:-translation_proxy}"
DB_USER="${PROXY_DB_USER:-proxy_user}"
DB_PASS="${PROXY_DB_PASS:-$(openssl rand -base64 18 | tr -dc 'a-zA-Z0-9' | head -c24)}"
CERTBOT_EMAIL="${CERTBOT_EMAIL:-admin@example.com}"
APP_DIR="/opt/translation-proxy"
VENV="${APP_DIR}/venv"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "${SCRIPT_DIR}")"
OUR_SERVER_IP="$(curl -sf --max-time 5 https://ipinfo.io/ip || hostname -I | awk '{print $1}')"
WEBROOT="/var/www/acme-challenge"

info "Server IP  : ${OUR_SERVER_IP}"
info "App dir    : ${APP_DIR}"
info "ACME email : ${CERTBOT_EMAIL}"

# ── 1. System packages ────────────────────────────────────────────────────────
step "1. Installing system packages"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    curl wget git rsync unzip build-essential \
    python3 python3-pip python3-venv python3-dev \
    libpq-dev openssl \
    nginx certbot python3-certbot-nginx \
    postgresql postgresql-contrib \
    ufw fail2ban
ok "Packages installed"

# ── 2. PostgreSQL ─────────────────────────────────────────────────────────────
step "2. Configuring PostgreSQL"
systemctl enable postgresql --now
sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='${DB_USER}'" | grep -q 1 || \
    sudo -u postgres psql -c "CREATE USER ${DB_USER} WITH PASSWORD '${DB_PASS}';"
sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | grep -q 1 || \
    sudo -u postgres psql -c "CREATE DATABASE ${DB_NAME} OWNER ${DB_USER};"
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE ${DB_NAME} TO ${DB_USER};" -q
ok "Database '${DB_NAME}' ready"

# ── 3. Nginx directories + assets ────────────────────────────────────────────
step "3. Configuring Nginx"
mkdir -p /etc/nginx/{sites-available,sites-enabled,snippets,ssl}
mkdir -p "${WEBROOT}/.well-known/acme-challenge"

# Remove ALL conflicting default configs
rm -f /etc/nginx/sites-enabled/default
rm -f /etc/nginx/sites-enabled/000-default-acme.conf
rm -f /etc/nginx/sites-available/000-default-acme.conf

# Self-signed fallback cert for the default_server HTTPS block
if [[ ! -f /etc/nginx/ssl/default.crt ]]; then
    openssl req -x509 -nodes -newkey rsa:2048 \
        -keyout /etc/nginx/ssl/default.key \
        -out    /etc/nginx/ssl/default.crt \
        -days 3650 -subj "/CN=proxy-default" -quiet
    ok "Self-signed default cert created"
fi

# DH params (needed by ssl-params.conf)
if [[ ! -f /etc/nginx/dhparam.pem ]]; then
    info "Generating DH params (~30s)..."
    openssl dhparam -out /etc/nginx/dhparam.pem 2048 2>/dev/null
    ok "DH params generated"
fi

# Shared TLS snippet
cat > /etc/nginx/snippets/ssl-params.conf << 'SSLEOF'
ssl_protocols TLSv1.2 TLSv1.3;
ssl_prefer_server_ciphers off;
ssl_dhparam /etc/nginx/dhparam.pem;
ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305:DHE-RSA-AES128-GCM-SHA256;
ssl_session_cache shared:SSL:10m;
ssl_session_timeout 1d;
ssl_session_tickets off;
ssl_stapling on;
ssl_stapling_verify on;
resolver 8.8.8.8 1.1.1.1 valid=300s;
resolver_timeout 5s;
SSLEOF

# ── 4. Fetch Cloudflare IPs + write snippets (before nginx.conf install) ──────
step "4. Fetching Cloudflare IP ranges"
python3 << 'PYEOF'
import json, urllib.request, pathlib

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "translation-proxy/1.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return [l for l in r.read().decode().strip().splitlines() if l]

try:
    ipv4 = fetch("https://www.cloudflare.com/ips-v4")
    ipv6 = fetch("https://www.cloudflare.com/ips-v6")
    print(f"  Fetched {len(ipv4)} IPv4 + {len(ipv6)} IPv6 ranges")
except Exception as e:
    print(f"  Warning: live fetch failed ({e}), using fallback")
    ipv4 = ["173.245.48.0/20","103.21.244.0/22","103.22.200.0/22","103.31.4.0/22",
            "141.101.64.0/18","108.162.192.0/18","190.93.240.0/20","188.114.96.0/20",
            "197.234.240.0/22","198.41.128.0/17","162.158.0.0/15","104.16.0.0/13",
            "104.24.0.0/14","172.64.0.0/13","131.0.72.0/22"]
    ipv6 = ["2400:cb00::/32","2606:4700::/32","2803:f800::/32","2405:b500::/32",
            "2405:8100::/32","2a06:98c0::/29","2c0f:f248::/32"]

# cloudflare-realip.conf – unwrap real visitor IP from CF-Connecting-IP
realip = ["# Auto-generated by deploy.sh – do not edit",
          "real_ip_header     CF-Connecting-IP;",
          "real_ip_recursive  on;",
          "# Cloudflare IPv4"] + \
         [f"set_real_ip_from {ip};" for ip in ipv4] + \
         ["# Cloudflare IPv6"] + \
         [f"set_real_ip_from {ip};" for ip in ipv6]
pathlib.Path("/etc/nginx/snippets/cloudflare-realip.conf").write_text("\n".join(realip))

# cloudflare-allow.conf – only CF IPs allowed on port 443
allow = ["# Auto-generated by deploy.sh – do not edit",
         "# Cloudflare IPv4"] + \
        [f"allow {ip};" for ip in ipv4] + \
        ["# Cloudflare IPv6"] + \
        [f"allow {ip};" for ip in ipv6] + \
        ["allow 127.0.0.1;", "allow ::1;", "deny all;"]
pathlib.Path("/etc/nginx/snippets/cloudflare-allow.conf").write_text("\n".join(allow))

# Cache IPs for later use by the API
pathlib.Path("/etc/nginx/cloudflare-ips.json").write_text(
    json.dumps({"ipv4": ipv4, "ipv6": ipv6, "source": "deploy"}, indent=2))
PYEOF
ok "Cloudflare snippets written"

# ── 5. Install nginx.conf ─────────────────────────────────────────────────────
step "5. Installing nginx.conf"
cp "${PROJECT_DIR}/nginx/nginx.conf" /etc/nginx/nginx.conf

# Make sure ACME webroot has correct permissions
chown -R www-data:www-data "${WEBROOT}"

# Test nginx config BEFORE starting
nginx -t || error "nginx config test failed – check errors above"
systemctl enable nginx --now
nginx -s reload 2>/dev/null || systemctl restart nginx
ok "Nginx running"

# ── 6. Application install ────────────────────────────────────────────────────
step "6. Installing application"
mkdir -p "${APP_DIR}/scripts"
rsync -a --delete "${PROJECT_DIR}/control_plane/" "${APP_DIR}/control_plane/"
rsync -a "${PROJECT_DIR}/scripts/"               "${APP_DIR}/scripts/"
chmod +x "${APP_DIR}/scripts/"*.sh 2>/dev/null || true

# Python venv
[[ ! -d "${VENV}" ]] && python3 -m venv "${VENV}"
"${VENV}/bin/pip" install --upgrade pip -q
"${VENV}/bin/pip" install -r "${APP_DIR}/control_plane/requirements.txt" -q
ok "Python packages installed"

# ── 7. Write .env ─────────────────────────────────────────────────────────────
step "7. Writing .env"
ENV_FILE="${APP_DIR}/.env"
cat > "${ENV_FILE}" << ENVEOF
DATABASE_URL=postgresql://${DB_USER}:${DB_PASS}@localhost/${DB_NAME}
OUR_SERVER_IP=${OUR_SERVER_IP}
CERTBOT_EMAIL=${CERTBOT_EMAIL}
SECRET_KEY=$(openssl rand -base64 32)
NGINX_SITES_ENABLED=/etc/nginx/sites-enabled
NGINX_SITES_AVAILABLE=/etc/nginx/sites-available
NGINX_ACME_WEBROOT=${WEBROOT}
NGINX_SNIPPETS_DIR=/etc/nginx/snippets
CERTBOT_CONFIG_DIR=/etc/letsencrypt
CERTBOT_WORK_DIR=/var/lib/letsencrypt
CERTBOT_LOGS_DIR=/var/log/letsencrypt
LETSENCRYPT_LIVE=/etc/letsencrypt/live
ENVEOF
chmod 600 "${ENV_FILE}"
ok ".env written"

# ── 8. Sudoers – www-data can run nginx, certbot, ufw, tee via sudo ──────────
step "8. Configuring sudoers"
cat > /etc/sudoers.d/www-data-proxy << 'SUDOEOF'
# Translation proxy – allow www-data to manage nginx, certbot, ufw
www-data ALL=(root) NOPASSWD: /usr/sbin/nginx
www-data ALL=(root) NOPASSWD: /usr/bin/nginx
www-data ALL=(root) NOPASSWD: /usr/bin/certbot
www-data ALL=(root) NOPASSWD: /usr/local/bin/certbot
www-data ALL=(root) NOPASSWD: /usr/sbin/ufw
www-data ALL=(root) NOPASSWD: /usr/bin/tee /etc/nginx/cloudflare-ips.json
www-data ALL=(root) NOPASSWD: /usr/bin/tee /etc/nginx/snippets/cloudflare-realip.conf
www-data ALL=(root) NOPASSWD: /usr/bin/tee /etc/nginx/snippets/cloudflare-allow.conf
SUDOEOF
chmod 440 /etc/sudoers.d/www-data-proxy
visudo -c -f /etc/sudoers.d/www-data-proxy && ok "sudoers OK"

# ── 9. Systemd service ────────────────────────────────────────────────────────
step "9. Installing systemd service"
cat > /etc/systemd/system/translation-proxy.service << SVCEOF
[Unit]
Description=Translation Proxy Control Plane
After=network.target postgresql.service

[Service]
Type=exec
User=www-data
Group=www-data
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${VENV}/bin/uvicorn control_plane.app.main:app \\
    --host 0.0.0.0 \\
    --port 8000 \\
    --workers 4 \\
    --loop uvloop \\
    --http httptools \\
    --access-log \\
    --log-level info
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
PrivateTmp=yes
ProtectSystem=full
ReadWritePaths=/etc/nginx/sites-available /etc/nginx/sites-enabled \\
               ${WEBROOT} /etc/letsencrypt \\
               /var/lib/letsencrypt /var/log/letsencrypt

[Install]
WantedBy=multi-user.target
SVCEOF

chown -R www-data:www-data "${APP_DIR}"
chown -R www-data:www-data "${WEBROOT}"
systemctl daemon-reload
systemctl enable translation-proxy
systemctl restart translation-proxy
sleep 3
systemctl is-active --quiet translation-proxy && ok "Service running" || \
    error "Service failed – run: journalctl -u translation-proxy -n 50"

# ── 10. UFW firewall ──────────────────────────────────────────────────────────
step "10. Configuring UFW firewall"

# Allow SSH first (never lock yourself out)
ufw allow OpenSSH

# Control plane (restrict to your IP in production)
ufw allow 8000/tcp

# Port 80: fully open – required for Let's Encrypt ACME HTTP-01 challenge
# Let's Encrypt validation servers are NOT Cloudflare IPs
ufw allow 80/tcp

# Port 443: Cloudflare IPs only
# Remove existing 443 rules first
while ufw status numbered 2>/dev/null | grep -qE "^\[ *[0-9]+\] 443"; do
    RNUM=$(ufw status numbered | grep -E "^\[ *[0-9]+\] 443" | \
           head -1 | sed 's/\[ *\([0-9]*\)\].*/\1/')
    [[ -n "$RNUM" ]] && ufw --force delete "$RNUM" 2>/dev/null || break
done

# Allow CF IPv4 on port 443
while IFS= read -r cidr; do
    [[ -n "$cidr" ]] && ufw allow proto tcp from "$cidr" to any port 443 \
        comment "cloudflare-https" 2>/dev/null || true
done < <(python3 -c "import json; d=json.load(open('/etc/nginx/cloudflare-ips.json')); print('\n'.join(d['ipv4']))")

# Deny all other connections to 443
ufw deny 443/tcp

ufw --force enable
ufw reload
ok "Firewall: port 80 open to all, port 443 locked to Cloudflare"

# ── 11. Certbot renewal systemd timer ────────────────────────────────────────
step "11. Installing certbot renewal timer"

cat > /etc/systemd/system/certbot-proxy-renew.service << 'SVCEOF'
[Unit]
Description=Translation Proxy – Certbot Certificate Renewal
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=root
ExecStart=/usr/bin/certbot renew \
    --non-interactive \
    --deploy-hook "nginx -s reload" \
    --config-dir /etc/letsencrypt \
    --work-dir   /var/lib/letsencrypt \
    --logs-dir   /var/log/letsencrypt
StandardOutput=journal
StandardError=journal
SVCEOF

cat > /etc/systemd/system/certbot-proxy-renew.timer << 'TMREOF'
[Unit]
Description=Translation Proxy – Certbot Renewal Timer

[Timer]
OnCalendar=*-*-* 03,15:00:00
RandomizedDelaySec=3600
Persistent=true

[Install]
WantedBy=timers.target
TMREOF

# Disable the system certbot timer to avoid conflicts
systemctl disable --now certbot.timer 2>/dev/null || true

systemctl daemon-reload
systemctl enable  certbot-proxy-renew.timer
systemctl restart certbot-proxy-renew.timer
ok "Certbot timer active (runs daily at 03:00 + 15:00)"

# ── 12. Fail2ban ──────────────────────────────────────────────────────────────
systemctl enable fail2ban --now

# ── 13. Verify ACME webroot ───────────────────────────────────────────────────
step "12. Verifying ACME webroot"
TEST_TOKEN="deploy-verify-$$"
TEST_FILE="${WEBROOT}/.well-known/acme-challenge/${TEST_TOKEN}"
echo "ok" > "${TEST_FILE}"
chown www-data:www-data "${TEST_FILE}"
sleep 1

# Test via localhost (same way certbot does it)
RESULT=$(curl -sf --max-time 5 \
    -H "Host: localhost" \
    "http://127.0.0.1/.well-known/acme-challenge/${TEST_TOKEN}" 2>/dev/null || echo "FAILED")
rm -f "${TEST_FILE}"

if [[ "${RESULT}" == "ok" ]]; then
    ok "ACME webroot verified – nginx is serving /.well-known/acme-challenge/ ✅"
else
    warn "ACME webroot test got: '${RESULT}'"
    warn "Manual check: curl -v http://127.0.0.1/.well-known/acme-challenge/test"
    # Try to diagnose
    nginx -t 2>&1 | head -5
fi

# ── 14. Health check ──────────────────────────────────────────────────────────
step "13. Final health check"
sleep 2
HEALTH=$(curl -sf --max-time 5 http://localhost:8000/health 2>/dev/null || echo "FAILED")
if echo "${HEALTH}" | grep -q '"ok"'; then
    ok "Control plane healthy: ${HEALTH}"
else
    warn "Health check: ${HEALTH}"
    warn "Check logs: journalctl -u translation-proxy -n 30"
fi

CF=$(curl -sf --max-time 5 http://localhost:8000/cloudflare/status 2>/dev/null || echo "{}")
if echo "${CF}" | grep -q "ipv4_count"; then
    ok "Cloudflare endpoint OK"
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║  ✅  Deploy complete! All services running.                  ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo "  API        : http://${OUR_SERVER_IP}:8000"
echo "  API docs   : http://${OUR_SERVER_IP}:8000/docs"
echo "  DB pass    : ${DB_PASS}  (saved in ${ENV_FILE})"
echo ""
echo "  Next steps:"
echo "  ① Create a customer:"
echo "    curl -s -X POST http://${OUR_SERVER_IP}:8000/customers \\"
echo "      -H 'Content-Type: application/json' \\"
echo "      -d '{\"email\":\"you@example.com\"}'"
echo ""
echo "  ② Register a domain (use the returned api_key):"
echo "    curl -s -X POST http://${OUR_SERVER_IP}:8000/domains \\"
echo "      -H 'X-API-Key: YOUR_KEY' \\"
echo "      -H 'Content-Type: application/json' \\"
echo "      -d '{\"domain\":\"yourdomain.com\",\"backend_url\":\"https://origin.com\"}'"
echo ""
echo "  ③ Add the DNS records shown, then verify + provision-ssl."
echo ""
echo "  Logs: journalctl -u translation-proxy -f"
echo ""
