#!/usr/bin/env bash
# ============================================================
# fix_permissions.sh
#
# Run this on an EXISTING installation to apply all the bug
# fixes related to file permissions and sudoers without
# re-running the full setup.sh.
#
# Usage:  sudo ./scripts/fix_permissions.sh
# ============================================================
set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info() { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC}  $*"; }

APP_DIR="${APP_DIR:-/opt/translation-proxy}"

# ── 1. Fix nginx directory ownership ────────────────────────
info "Fixing nginx directory ownership..."
chown www-data:www-data /etc/nginx/sites-available
chown www-data:www-data /etc/nginx/sites-enabled
chown www-data:www-data /etc/nginx/snippets 2>/dev/null || true
chown -R www-data:www-data /var/www/acme-challenge
info "Nginx dirs: www-data can now write configs directly."

# ── 2. Replace sudoers file with complete version ────────────
info "Updating sudoers file..."
SUDOERS_FILE="/etc/sudoers.d/www-data-proxy"
cat > "${SUDOERS_FILE}" << 'SUDOEOF'
# Translation proxy – allow app user to manage nginx, certbot, ufw, and
# write nginx config files (sites-available/enabled are root-owned by default)
www-data ALL=(root) NOPASSWD: /usr/sbin/nginx
www-data ALL=(root) NOPASSWD: /usr/bin/nginx
www-data ALL=(root) NOPASSWD: /usr/bin/certbot
www-data ALL=(root) NOPASSWD: /usr/local/bin/certbot
www-data ALL=(root) NOPASSWD: /usr/sbin/ufw
www-data ALL=(root) NOPASSWD: /usr/bin/ufw

# Write nginx cloudflare snippets
www-data ALL=(root) NOPASSWD: /usr/bin/tee /etc/nginx/cloudflare-ips.json
www-data ALL=(root) NOPASSWD: /usr/bin/tee /etc/nginx/snippets/cloudflare-realip.conf
www-data ALL=(root) NOPASSWD: /usr/bin/tee /etc/nginx/snippets/cloudflare-allow.conf
www-data ALL=(root) NOPASSWD: /usr/bin/tee /etc/nginx/snippets/ssl-params.conf

# Write per-customer nginx vhost configs
www-data ALL=(root) NOPASSWD: /usr/bin/tee /etc/nginx/sites-available/*

# Symlink/remove for sites-enabled management
www-data ALL=(root) NOPASSWD: /usr/bin/ln
www-data ALL=(root) NOPASSWD: /bin/ln
www-data ALL=(root) NOPASSWD: /usr/bin/rm
www-data ALL=(root) NOPASSWD: /bin/rm
www-data ALL=(root) NOPASSWD: /usr/bin/mkdir
www-data ALL=(root) NOPASSWD: /bin/mkdir

# Fix cert permissions after issuance/renewal
www-data ALL=(root) NOPASSWD: /usr/bin/chmod
www-data ALL=(root) NOPASSWD: /bin/chmod

# pgrep for health checks
www-data ALL=(root) NOPASSWD: /usr/bin/pgrep
SUDOEOF
chmod 440 "${SUDOERS_FILE}"
visudo -c -f "${SUDOERS_FILE}" && info "Sudoers updated OK." || {
    echo "ERROR: sudoers syntax check failed — restoring old file"
    rm -f "${SUDOERS_FILE}"
    exit 1
}

# ── 3. Fix letsencrypt cert permissions (if any certs exist) ─
LIVE_DIR="/etc/letsencrypt/live"
if [[ -d "${LIVE_DIR}" ]]; then
    info "Fixing letsencrypt certificate permissions..."
    chmod 755 "${LIVE_DIR}" 2>/dev/null || true
    chmod 755 /etc/letsencrypt/archive 2>/dev/null || true
    for domain_dir in "${LIVE_DIR}"/*/; do
        [[ -d "${domain_dir}" ]] || continue
        domain="$(basename "${domain_dir}")"
        [[ "${domain}" == "README" ]] && continue
        chmod -R 755 "${domain_dir}" 2>/dev/null || true
        chmod -R 755 "/etc/letsencrypt/archive/${domain}" 2>/dev/null || true
        info "  Fixed: ${domain}"
    done
fi

# ── 4. Restart the app ───────────────────────────────────────
info "Restarting translation-proxy service..."
systemctl restart translation-proxy && info "Service restarted." || \
    warn "Could not restart service — do it manually: systemctl restart translation-proxy"

echo ""
echo -e "${GREEN}✓ Permissions fixed.${NC}"
echo ""
echo "  Now re-run provision-ssl for any domains that failed:"
echo "    BASE=http://localhost:8000"
echo "    curl -s -X POST \"\$BASE/domains/YOURDOMAIN/provision-ssl\" \\"
echo "         -H \"X-API-Key: YOUR_KEY\" | python3 -m json.tool"
echo ""
echo "  If cloudflare snippets are missing, also run:"
echo "    curl -s -X POST \"\$BASE/cloudflare/refresh\" | python3 -m json.tool"
