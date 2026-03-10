#!/usr/bin/env bash
# Update Cloudflare IP ranges in nginx + UFW.
# Run daily via cron: 0 4 * * * root /opt/translation-proxy/scripts/update_cloudflare_ips.sh
set -euo pipefail

LOG_PREFIX="[$(date '+%Y-%m-%d %H:%M:%S')] cf-ip-update"
BASE_URL="${PROXY_BASE_URL:-http://localhost:8000}"

echo "${LOG_PREFIX} Refreshing Cloudflare IPs..."

RESPONSE=$(curl -sf -X POST "${BASE_URL}/cloudflare/refresh?update_ufw=true" \
    -H "Content-Type: application/json" \
    --max-time 60 2>&1) || {
    echo "${LOG_PREFIX} ERROR: API call failed: ${RESPONSE}"
    exit 1
}

echo "${LOG_PREFIX} Response: ${RESPONSE}"
echo "${LOG_PREFIX} Done."
