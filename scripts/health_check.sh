#!/usr/bin/env bash
# Quick health check – returns 0 on success, 1 on failure.
# Use with monitoring tools (Zabbix, Prometheus blackbox, etc.).

set -euo pipefail

BASE_URL="${PROXY_BASE_URL:-http://localhost:8000}"
ERRORS=0

check() {
    local name="$1" url="$2" expected="$3"
    local status
    status=$(curl -sf -o /dev/null -w "%{http_code}" "${url}" 2>/dev/null || echo "000")
    if [[ "${status}" == "${expected}" ]]; then
        echo "✅ ${name}: HTTP ${status}"
    else
        echo "❌ ${name}: HTTP ${status} (expected ${expected})"
        ERRORS=$((ERRORS + 1))
    fi
}

echo "=== Translation Proxy Health Check ==="
check "Control Plane /health"     "${BASE_URL}/health"       200
check "Control Plane /nginx/status" "${BASE_URL}/nginx/status" 200
check "Nginx (HTTP)"              "http://localhost:80"       444   # default_server returns 444
check "Nginx (HTTPS default)"     "https://localhost:443"     000   # SSL connect error is expected for unknown domain

echo ""
if [[ ${ERRORS} -eq 0 ]]; then
    echo "All checks passed."
    exit 0
else
    echo "${ERRORS} check(s) failed."
    exit 1
fi
