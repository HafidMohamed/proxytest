#!/usr/bin/env bash
# Renew Let's Encrypt certificates and reload nginx.
# Called by cron twice daily.
set -euo pipefail

LOG_PREFIX="[$(date '+%Y-%m-%d %H:%M:%S')] certbot-renew"

echo "${LOG_PREFIX} Starting renewal check..."

# Renew certs expiring within 30 days.
# The --deploy-hook reloads nginx after any successful renewal.
if certbot renew \
    --non-interactive \
    --deploy-hook "nginx -s reload" \
    --quiet; then
    echo "${LOG_PREFIX} Renewal check complete."
else
    echo "${LOG_PREFIX} ERROR: certbot renew exited with code $?"
fi

# Update expiry dates in the database via the control plane API
# (optional – the API /ssl/renew-all endpoint does the same thing)
# curl -sf -X POST http://localhost:8000/ssl/renew-all || true

echo "${LOG_PREFIX} Done."
