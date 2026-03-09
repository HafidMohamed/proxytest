#!/usr/bin/env bash
# ============================================================
# Translation Proxy – Server Setup Script
# Ubuntu 22.04 / 24.04 LTS
# Run as root or with sudo.
# Usage:
#   chmod +x scripts/setup.sh
#   sudo ./scripts/setup.sh
# ============================================================
set -euo pipefail

# ── Colours ──────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ── Config (override via env) ─────────────────────────────────
DB_NAME="${PROXY_DB_NAME:-translation_proxy}"
DB_USER="${PROXY_DB_USER:-proxy_user}"
DB_PASS="${PROXY_DB_PASS:-$(openssl rand -base64 24)}"
CERTBOT_EMAIL="${CERTBOT_EMAIL:-admin@example.com}"
APP_DIR="${APP_DIR:-/opt/translation-proxy}"
VENV_DIR="${APP_DIR}/venv"
OUR_SERVER_IP="$(curl -sf https://ipinfo.io/ip || hostname -I | awk '{print $1}')"

info "Server public IP: ${OUR_SERVER_IP}"
info "App directory:    ${APP_DIR}"

# ── 1. System updates ─────────────────────────────────────────
info "Updating system packages..."
apt-get update -qq
apt-get install -y -qq \
    curl wget git unzip build-essential \
    python3 python3-pip python3-venv python3-dev \
    libpq-dev openssl \
    nginx certbot python3-certbot-nginx \
    postgresql postgresql-contrib \
    ufw fail2ban

# ── 2. PostgreSQL ─────────────────────────────────────────────
info "Configuring PostgreSQL..."
systemctl enable postgresql --now

# Create DB user and database (idempotent)
sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='${DB_USER}'" | grep -q 1 || \
    sudo -u postgres psql -c "CREATE USER ${DB_USER} WITH PASSWORD '${DB_PASS}';"

sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | grep -q 1 || \
    sudo -u postgres psql -c "CREATE DATABASE ${DB_NAME} OWNER ${DB_USER};"

sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE ${DB_NAME} TO ${DB_USER};"

info "PostgreSQL: database '${DB_NAME}' and user '${DB_USER}' ready."

# ── 3. Nginx ──────────────────────────────────────────────────
info "Configuring Nginx..."

# Create required directories
mkdir -p /var/www/acme-challenge
mkdir -p /etc/nginx/sites-available
mkdir -p /etc/nginx/sites-enabled
mkdir -p /etc/nginx/snippets
mkdir -p /etc/nginx/ssl

# Remove default site
rm -f /etc/nginx/sites-enabled/default

# Copy our main nginx.conf
cp "$(dirname "$0")/../nginx/nginx.conf" /etc/nginx/nginx.conf

# Generate a self-signed default certificate (used by the default_server block)
# This prevents nginx from failing to start before any customer certs exist.
if [[ ! -f /etc/nginx/ssl/default.crt ]]; then
    info "Generating self-signed default certificate..."
    openssl req -x509 -nodes -newkey rsa:2048 \
        -keyout /etc/nginx/ssl/default.key \
        -out    /etc/nginx/ssl/default.crt \
        -days   3650 \
        -subj   "/CN=translation-proxy-default"
fi

# Generate DH params (used by ssl-params.conf)
if [[ ! -f /etc/nginx/dhparam.pem ]]; then
    info "Generating DH params (this takes ~30s)..."
    openssl dhparam -out /etc/nginx/dhparam.pem 2048
fi

# Write ssl-params snippet
cat > /etc/nginx/snippets/ssl-params.conf << 'EOF'
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
EOF

# Test and start nginx
nginx -t && systemctl enable nginx --now && systemctl reload nginx
info "Nginx is running."

# ── 4. Application ────────────────────────────────────────────
info "Installing application to ${APP_DIR}..."
mkdir -p "${APP_DIR}"

# Copy project files
rsync -a --delete "$(dirname "$0")/../control_plane/" "${APP_DIR}/control_plane/"

# Python virtual env
if [[ ! -d "${VENV_DIR}" ]]; then
    python3 -m venv "${VENV_DIR}"
fi
"${VENV_DIR}/bin/pip" install --upgrade pip -q
"${VENV_DIR}/bin/pip" install -r "${APP_DIR}/control_plane/requirements.txt" -q

# ── 5. .env file ──────────────────────────────────────────────
ENV_FILE="${APP_DIR}/.env"
if [[ ! -f "${ENV_FILE}" ]]; then
    info "Writing .env file..."
    cat > "${ENV_FILE}" << EOF
DATABASE_URL=postgresql://${DB_USER}:${DB_PASS}@localhost/${DB_NAME}
OUR_SERVER_IP=${OUR_SERVER_IP}
CERTBOT_EMAIL=${CERTBOT_EMAIL}
SECRET_KEY=$(openssl rand -base64 32)
NGINX_SITES_ENABLED=/etc/nginx/sites-enabled
NGINX_SITES_AVAILABLE=/etc/nginx/sites-available
NGINX_ACME_WEBROOT=/var/www/acme-challenge
NGINX_SNIPPETS_DIR=/etc/nginx/snippets
LETSENCRYPT_LIVE=/etc/letsencrypt/live
EOF
    chmod 600 "${ENV_FILE}"
    info ".env written to ${ENV_FILE}"
else
    warn ".env already exists – skipping generation. Update OUR_SERVER_IP if needed."
fi

# ── 6. Systemd service ────────────────────────────────────────
info "Installing systemd service..."
cat > /etc/systemd/system/translation-proxy.service << EOF
[Unit]
Description=Translation Proxy Control Plane
After=network.target postgresql.service

[Service]
Type=exec
User=www-data
Group=www-data
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${VENV_DIR}/bin/uvicorn control_plane.app.main:app \
    --host 0.0.0.0 \
    --port 8000 \
    --workers 4 \
    --loop uvloop \
    --http httptools \
    --access-log \
    --log-level info
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

# Security hardening
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ReadWritePaths=/etc/nginx/sites-available /etc/nginx/sites-enabled /var/www/acme-challenge /etc/letsencrypt

[Install]
WantedBy=multi-user.target
EOF

# Give www-data permission to reload nginx without a password
SUDOERS_FILE="/etc/sudoers.d/www-data-nginx"
if [[ ! -f "${SUDOERS_FILE}" ]]; then
    echo "www-data ALL=(root) NOPASSWD: /usr/sbin/nginx, /bin/nginx" > "${SUDOERS_FILE}"
    chmod 440 "${SUDOERS_FILE}"
fi

chown -R www-data:www-data "${APP_DIR}"
chown -R www-data:www-data /var/www/acme-challenge

systemctl daemon-reload
systemctl enable translation-proxy --now
info "translation-proxy service started."

# ── 7. Certbot renewal cron ───────────────────────────────────
info "Setting up certbot auto-renewal..."
CRON_FILE="/etc/cron.d/certbot-renew"
cat > "${CRON_FILE}" << 'EOF'
# Renew Let's Encrypt certs twice daily (certbot skips if not due)
0 3,15 * * * root /opt/translation-proxy/scripts/renew_certs.sh >> /var/log/certbot-renew.log 2>&1
EOF

# ── 8. Firewall ───────────────────────────────────────────────
info "Configuring UFW firewall..."
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow 8000/tcp   # control plane (restrict to your IPs in production!)
ufw --force enable
info "Firewall configured."

# ── 9. Fail2ban ───────────────────────────────────────────────
info "Enabling fail2ban..."
systemctl enable fail2ban --now

# ── Done ──────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║   Translation Proxy setup complete!                  ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════╝${NC}"
echo ""
echo "  Control plane API : http://${OUR_SERVER_IP}:8000"
echo "  API docs          : http://${OUR_SERVER_IP}:8000/docs"
echo "  DB credentials    : ${ENV_FILE}"
echo ""
echo "  ⚠️  IMPORTANT: Restrict port 8000 to your IP in production!"
echo "  ⚠️  Update CERTBOT_EMAIL in ${ENV_FILE} before issuing certs."
echo ""
