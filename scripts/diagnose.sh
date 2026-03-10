#!/usr/bin/env bash
# ================================================================
# Translation Proxy – End-to-end diagnostic
# Checks every step of the visitor → proxy → backend chain.
#
# Usage:  bash scripts/diagnose.sh cafezisimoukreuzau.de
# ================================================================
set -euo pipefail

DOMAIN="${1:-}"
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
ok()   { echo -e "  ${GREEN}✅ $*${NC}"; }
fail() { echo -e "  ${RED}❌ $*${NC}"; }
warn() { echo -e "  ${YELLOW}⚠️  $*${NC}"; }
info() { echo -e "  $*"; }

[[ -z "$DOMAIN" ]] && { echo "Usage: bash diagnose.sh <domain>"; exit 1; }

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Proxy Diagnostic: $DOMAIN"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

SERVER_IP="$(curl -sf --max-time 5 https://ipinfo.io/ip || hostname -I | awk '{print $1}')"

# ── 1. DNS check ──────────────────────────────────────────────────────────────
echo ""
echo "1. DNS"
RESOLVED_IP="$(dig +short A "$DOMAIN" | tail -1)"
info "Domain resolves to: ${RESOLVED_IP:-NONE}"

# Check if resolved IP is a Cloudflare IP
CF_IP_RANGES=$(python3 -c "
import json, pathlib
try:
    d = json.loads(pathlib.Path('/etc/nginx/cloudflare-ips.json').read_text())
    print('\n'.join(d.get('ipv4',[])))
except: pass
" 2>/dev/null)

IS_CF_IP=false
if [[ -n "$RESOLVED_IP" ]]; then
    while IFS= read -r cidr; do
        if python3 -c "
import ipaddress
try:
    print('yes' if ipaddress.ip_address('$RESOLVED_IP') in ipaddress.ip_network('$cidr') else '', end='')
except: pass
" 2>/dev/null | grep -q yes; then
            IS_CF_IP=true
            break
        fi
    done <<< "$CF_IP_RANGES"
fi

if [[ "$RESOLVED_IP" == "$SERVER_IP" ]]; then
    fail "Domain resolves to YOUR SERVER IP directly ($SERVER_IP)"
    fail "Cloudflare orange cloud proxy is NOT enabled"
    echo ""
    echo -e "  ${RED}━━━ ACTION REQUIRED ━━━${NC}"
    echo "  1. Go to Cloudflare DNS dashboard for $DOMAIN"
    echo "  2. Find the A record pointing to $SERVER_IP"
    echo "  3. Click the grey cloud icon → turn it ORANGE (Proxied)"
    echo "  4. Save. Wait 1-2 minutes for propagation."
    echo "  5. Re-run this diagnostic."
    echo ""
    echo "  With grey cloud (DNS only):"
    echo "    Browser → $SERVER_IP:443 → UFW DROPS (not a CF IP) → timeout"
    echo ""
    echo "  With orange cloud (Proxied):"
    echo "    Browser → Cloudflare IP:443 → CF edge → $SERVER_IP:443 → backend"
    echo ""
elif $IS_CF_IP; then
    ok "Domain resolves to Cloudflare IP ($RESOLVED_IP) — orange cloud is ON ✅"
elif [[ -z "$RESOLVED_IP" ]]; then
    fail "Domain does not resolve — A record missing or not propagated yet"
else
    warn "Domain resolves to $RESOLVED_IP (unknown IP — may be CF or other proxy)"
fi

# ── 2. Nginx config ───────────────────────────────────────────────────────────
echo ""
echo "2. Nginx config"

CONF="/etc/nginx/sites-enabled/${DOMAIN}.conf"
HTTP_CONF="/etc/nginx/sites-enabled/${DOMAIN}.http.conf"
AVAIL="/etc/nginx/sites-available/${DOMAIN}.conf"

if [[ -f "$CONF" ]]; then
    ok "SSL config exists: $CONF"
    BACKEND=$(grep "proxy_pass" "$CONF" | head -1 | awk '{print $2}' | tr -d ';')
    info "  → proxying to: $BACKEND"
elif [[ -f "$HTTP_CONF" ]]; then
    warn "Only HTTP config exists — provision-ssl has not run yet"
    info "  Run: curl -X POST \$BASE/domains/$DOMAIN/provision-ssl -H 'X-API-Key: \$API_KEY'"
elif [[ -f "$AVAIL" ]]; then
    fail "Config exists in sites-available but NOT symlinked to sites-enabled"
    info "  Fix: ln -sf $AVAIL /etc/nginx/sites-enabled/${DOMAIN}.conf && nginx -s reload"
else
    fail "No nginx config found for $DOMAIN"
    info "  Register and provision-ssl via the API first"
fi

# ── 3. Nginx syntax and running ───────────────────────────────────────────────
echo ""
echo "3. Nginx status"
if nginx -t 2>&1 | grep -q "syntax is ok"; then
    ok "nginx config syntax OK"
else
    fail "nginx config has errors:"
    nginx -t 2>&1 | grep -v "^$" | head -10 | while read l; do info "  $l"; done
fi

if systemctl is-active --quiet nginx; then
    ok "nginx is running"
else
    fail "nginx is NOT running — run: systemctl start nginx"
fi

# ── 4. SSL certificate ────────────────────────────────────────────────────────
echo ""
echo "4. SSL certificate"
CERT_PATH="/etc/letsencrypt/live/${DOMAIN}/fullchain.pem"
KEY_PATH="/etc/letsencrypt/live/${DOMAIN}/privkey.pem"

if [[ -f "$CERT_PATH" ]]; then
    ok "Certificate exists: $CERT_PATH"
    EXPIRY=$(openssl x509 -enddate -noout -in "$CERT_PATH" 2>/dev/null | cut -d= -f2)
    info "  Expires: $EXPIRY"
    # Check if www-data can read it
    if sudo -u www-data test -r "$CERT_PATH" 2>/dev/null; then
        ok "www-data can read the certificate"
    else
        fail "www-data CANNOT read the certificate — fixing permissions..."
        chmod 755 /etc/letsencrypt/live /etc/letsencrypt/archive 2>/dev/null || true
        chmod -R 755 "/etc/letsencrypt/live/${DOMAIN}" 2>/dev/null || true
        chmod -R 755 "/etc/letsencrypt/archive/${DOMAIN}" 2>/dev/null || true
        ok "Permissions fixed. Retrying..."
        sudo -u www-data test -r "$CERT_PATH" && ok "Now readable" || fail "Still not readable"
    fi
else
    fail "No certificate found at $CERT_PATH"
    info "  Run: curl -X POST \$BASE/domains/$DOMAIN/provision-ssl -H 'X-API-Key: \$API_KEY'"
fi

# ── 5. ACME webroot ───────────────────────────────────────────────────────────
echo ""
echo "5. ACME webroot (for future cert renewals)"
WEBROOT="/var/www/acme-challenge"
echo "ok" > "${WEBROOT}/.well-known/acme-challenge/diag-test-$$" 2>/dev/null || true
RESULT=$(curl -sf -H "Host: $DOMAIN" "http://127.0.0.1/.well-known/acme-challenge/diag-test-$$" 2>/dev/null || echo "FAILED")
rm -f "${WEBROOT}/.well-known/acme-challenge/diag-test-$$"
if [[ "$RESULT" == "ok" ]]; then
    ok "ACME webroot served correctly on port 80"
else
    fail "ACME webroot not working (cert renewals will fail): got '$RESULT'"
fi

# ── 6. Backend reachability ───────────────────────────────────────────────────
echo ""
echo "6. Backend reachability"
if [[ -n "${BACKEND:-}" ]]; then
    info "Testing connection to backend: $BACKEND"
    HTTP_CODE=$(curl -sk -o /dev/null -w "%{http_code}" \
        --max-time 10 \
        -H "Host: $(echo $BACKEND | sed 's|https\?://||' | cut -d/ -f1)" \
        "$BACKEND" 2>/dev/null || echo "000")
    if [[ "$HTTP_CODE" == "000" ]]; then
        fail "Cannot connect to backend $BACKEND — connection timed out or refused"
        info "  Check: is the backend server running and reachable from this server?"
    elif [[ "$HTTP_CODE" =~ ^[2345] ]]; then
        ok "Backend responded: HTTP $HTTP_CODE"
    else
        warn "Backend returned HTTP $HTTP_CODE"
    fi
else
    warn "No backend URL found (nginx config not yet written)"
fi

# ── 7. UFW firewall ───────────────────────────────────────────────────────────
echo ""
echo "7. Firewall rules"
ufw status numbered 2>/dev/null | grep -E "80|443|ssh|22" | while read line; do
    info "  $line"
done
CF_RULES=$(ufw status numbered 2>/dev/null | grep -c "cloudflare-https" || echo 0)
if [[ "$CF_RULES" -ge 10 ]]; then
    ok "Port 443 locked to Cloudflare IPs ($CF_RULES rules)"
else
    warn "Cloudflare UFW rules may be missing (found $CF_RULES)"
fi
if ufw status 2>/dev/null | grep -q "^80.*ALLOW"; then
    ok "Port 80 open to all (required for ACME / cert renewal)"
else
    fail "Port 80 NOT open — run: ufw allow 80/tcp"
fi

# ── 8. End-to-end local test ──────────────────────────────────────────────────
echo ""
echo "8. Local proxy test (simulates nginx → backend)"
if [[ -n "${BACKEND:-}" && -f "$CERT_PATH" ]]; then
    # Test that nginx is actually proxying — connect via localhost with SNI
    PROXY_CODE=$(curl -sk -o /dev/null -w "%{http_code}" \
        --resolve "${DOMAIN}:443:127.0.0.1" \
        --max-time 10 \
        "https://${DOMAIN}/" 2>/dev/null || echo "000")
    if [[ "$PROXY_CODE" == "000" ]]; then
        fail "Proxy test failed — nginx not responding on 443 locally"
    elif [[ "$PROXY_CODE" =~ ^[2345] ]]; then
        ok "Proxy returned HTTP $PROXY_CODE from backend via nginx ✅"
        info "  The proxy is working correctly!"
    else
        warn "Proxy returned HTTP $PROXY_CODE"
    fi
else
    warn "Skipping proxy test (cert or config not ready)"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Summary"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
if $IS_CF_IP; then
    echo -e "  ${GREEN}Cloudflare proxy is ON. Visitor flow:${NC}"
    echo "  Browser → Cloudflare → $SERVER_IP:443 → nginx → $BACKEND"
elif [[ "$RESOLVED_IP" == "$SERVER_IP" ]]; then
    echo -e "  ${RED}⚠️  CLOUDFLARE PROXY IS OFF (grey cloud)${NC}"
    echo "  Visitors CANNOT reach the site. Enable orange cloud in CF DNS."
fi
echo ""
