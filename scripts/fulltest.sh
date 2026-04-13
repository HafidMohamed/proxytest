#!/usr/bin/env bash
BASE=http://localhost:8000

echo "=========================================="
echo "STEP 1: Get existing API key from database"
echo "=========================================="
API_KEY=$(docker exec proxytest_postgres_1 psql -U proxy_user -d translation_proxy \
  -t -c "SELECT api_key FROM customers WHERE email='info@cafezisimoukreuzau.de';" | tr -d ' \n')

# If not found, create the customer
if [ -z "$API_KEY" ]; then
  echo "Customer not found, creating..."
  RESP=$(curl -s -X POST "$BASE/customers" \
    -H "Content-Type: application/json" \
    -d '{"email":"info@cafezisimoukreuzau.de"}')
  echo $RESP | python3 -m json.tool
  API_KEY=$(echo $RESP | python3 -c "import sys,json; print(json.load(sys.stdin)['api_key'])")
fi

echo "API_KEY=$API_KEY"
echo ""

echo "=========================================="
echo "STEP 2: Register domain"
echo "=========================================="
curl -s -X POST "$BASE/domains" \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"domain":"cafezisimoukreuzau.de","backend_url":"https://208.67.222.222"}' \
  | python3 -m json.tool
echo ""

echo "=========================================="
echo "STEP 3: Check domain status"
echo "=========================================="
curl -s "$BASE/domains/cafezisimoukreuzau.de" \
  -H "X-API-Key: $API_KEY" \
  | python3 -m json.tool
echo ""

echo "=========================================="
echo "STEP 4: Verify domain DNS"
echo "=========================================="
curl -s -X POST "$BASE/domains/cafezisimoukreuzau.de/verify" \
  -H "X-API-Key: $API_KEY" \
  | python3 -m json.tool
echo ""

echo "=========================================="
echo "STEP 5: Provision SSL"
echo "=========================================="
curl -s -X POST "$BASE/domains/cafezisimoukreuzau.de/provision-ssl" \
  -H "X-API-Key: $API_KEY" \
  | python3 -m json.tool
echo ""

echo "=========================================="
echo "STEP 6: Full debug check"
echo "=========================================="
curl -s "$BASE/debug/domain/cafezisimoukreuzau.de" \
  | python3 -m json.tool
echo ""

echo "=========================================="
echo "STEP 7: Cloudflare refresh"
echo "=========================================="
curl -s -X POST "$BASE/cloudflare/refresh" \
  | python3 -m json.tool
echo ""

echo "=========================================="
echo "STEP 8: Test visitor access"
echo "=========================================="
curl -s -H "Host: cafezisimoukreuzau.de" https://204.168.150.79/ -k
echo ""

echo "=========================================="
echo "STEP 9: Nginx status"
echo "=========================================="
curl -s "$BASE/nginx/status" | python3 -m json.tool
