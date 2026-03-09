# Translation Proxy – Full Deployment Guide
## Ubuntu 22.04 / 24.04 LTS – Production Setup with Real Internet Testing

---

## Table of Contents
1. [Architecture Overview](#1-architecture-overview)
2. [Prerequisites](#2-prerequisites)
3. [Server Provisioning](#3-server-provisioning)
4. [Deploy the Application](#4-deploy-the-application)
5. [Configure DNS (Your Control Domain)](#5-configure-dns-your-control-domain)
6. [End-to-End Flow Test](#6-end-to-end-flow-test)
7. [Load Testing & Error Rate Measurement](#7-load-testing--error-rate-measurement)
8. [Scaling Guide](#8-scaling-guide)
9. [Monitoring & Maintenance](#9-monitoring--maintenance)
10. [Troubleshooting](#10-troubleshooting)

---

## 1. Architecture Overview

```
Customer's visitor / bot
         │
         ▼ DNS: customer-domain.com → YOUR_SERVER_IP
   ┌─────────────────────────────────────────────────────┐
   │  Nginx (port 80 / 443)                              │
   │  ┌───────────────────────────────────────────────┐  │
   │  │  default_server (port 443, unknown domain)    │  │
   │  │  → return 444  (silent drop)                  │  │
   │  └───────────────────────────────────────────────┘  │
   │  ┌───────────────────────────────────────────────┐  │
   │  │  server { server_name customer-domain.com; }  │  │
   │  │  ssl_certificate /etc/letsencrypt/live/...    │  │
   │  │  proxy_pass https://customer-origin.com       │  │
   │  └───────────────────────────────────────────────┘  │
   └─────────────────────────────────────────────────────┘
         │
         ▼  (after nginx proxy)
   Customer's origin server (backend_url)

Control Plane API (port 8000 – internal / restricted)
   ├── POST /customers            ← register customer
   ├── POST /domains              ← get DNS instructions
   ├── POST /domains/{d}/verify   ← check TXT + A record
   ├── POST /domains/{d}/provision-ssl  ← run certbot + write nginx
   └── DELETE /domains/{d}        ← remove domain
```

---

## 2. Prerequisites

- Ubuntu 22.04 or 24.04 server (fresh recommended)
- Root or sudo access
- A **domain you control** for the control plane API (e.g., `api.yourproxy.com`)
- Your server's **public IPv4 address**
- Git installed locally

---

## 3. Server Provisioning

### 3.1 Create a VPS

Recommended specs:
| Traffic       | CPU  | RAM  | Storage |
|---------------|------|------|---------|
| Dev / test    | 1    | 1 GB | 20 GB   |
| ~100 domains  | 2    | 4 GB | 40 GB   |
| ~1000 domains | 4    | 8 GB | 80 GB   |
| ~10k domains  | 8    | 16 GB| 160 GB  |

Good providers: DigitalOcean, Hetzner, Vultr, AWS EC2, Linode.

### 3.2 SSH into the server

```bash
ssh root@YOUR_SERVER_IP
```

### 3.3 Set hostname

```bash
hostnamectl set-hostname translation-proxy
```

---

## 4. Deploy the Application

### 4.1 Clone the repository

```bash
git clone https://github.com/yourorg/translation-proxy.git /opt/translation-proxy-src
cd /opt/translation-proxy-src
```

### 4.2 Run the setup script

```bash
export CERTBOT_EMAIL="your@email.com"
export PROXY_DB_PASS="a-strong-password-here"

chmod +x scripts/setup.sh
sudo -E ./scripts/setup.sh
```

The setup script will:
- Install Nginx, PostgreSQL, Certbot, Python 3
- Create the database and user
- Generate a self-signed fallback cert (so nginx starts before any real cert exists)
- Generate DH params for TLS
- Install the FastAPI control plane as a systemd service
- Configure UFW firewall
- Set up certbot auto-renewal via cron

### 4.3 Verify the service is running

```bash
systemctl status translation-proxy
journalctl -u translation-proxy -f
```

You should see:
```
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8000
```

### 4.4 Test the health endpoint

```bash
curl http://YOUR_SERVER_IP:8000/health
# {"status":"ok","timestamp":"2025-01-15T10:00:00"}
```

### 4.5 View the API docs

Open in browser: `http://YOUR_SERVER_IP:8000/docs`

---

## 5. Configure DNS (Your Control Domain)

You need an A record pointing your API domain to your server:

```
api.yourproxy.com.  A  YOUR_SERVER_IP
```

Then issue a cert for your API (optional but recommended):

```bash
certbot certonly --nginx -d api.yourproxy.com \
    --email your@email.com --agree-tos --non-interactive
```

---

## 6. End-to-End Flow Test

This section walks through the complete customer onboarding flow using real HTTP calls.

### 6.1 Register a customer

```bash
BASE="http://YOUR_SERVER_IP:8000"

# Create a customer account
RESPONSE=$(curl -s -X POST "${BASE}/customers" \
  -H "Content-Type: application/json" \
  -d '{"email": "alice@example.com"}')

echo "$RESPONSE" | python3 -m json.tool

# Extract the API key
API_KEY=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['api_key'])")
echo "API Key: $API_KEY"
```

### 6.2 Register a domain

```bash
# Replace with a real domain you own
CUSTOMER_DOMAIN="www.alice-website.com"
ORIGIN_URL="https://alice-website.com"    # where we proxy TO

RESP=$(curl -s -X POST "${BASE}/domains" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d "{\"domain\": \"${CUSTOMER_DOMAIN}\", \"backend_url\": \"${ORIGIN_URL}\"}")

echo "$RESP" | python3 -m json.tool
```

You will receive something like:

```json
{
  "domain": "www.alice-website.com",
  "txt_record_name": "_proxy-verify.www.alice-website.com",
  "txt_record_value": "proxy-verify=abc123xyz...",
  "a_record_value": "YOUR_SERVER_IP",
  "instructions": "Add these two DNS records..."
}
```

### 6.3 Add DNS records at your registrar

Go to your domain registrar (Cloudflare, Namecheap, GoDaddy, etc.) and add:

| Type | Name                               | Value                        |
|------|------------------------------------|------------------------------|
| TXT  | `_proxy-verify.www.alice-website.com` | `proxy-verify=abc123xyz...`  |
| A    | `www.alice-website.com`            | `YOUR_SERVER_IP`             |

Wait 1–5 minutes for propagation, then verify:

```bash
# Check TXT record
dig TXT _proxy-verify.www.alice-website.com +short

# Check A record
dig A www.alice-website.com +short
```

### 6.4 Verify domain ownership

```bash
curl -s -X POST "${BASE}/domains/${CUSTOMER_DOMAIN}/verify" \
  -H "X-API-Key: $API_KEY" | python3 -m json.tool
```

Expected response:
```json
{
  "message": "Domain verified successfully. Now call /provision-ssl to issue your certificate.",
  "detail": "..."
}
```

### 6.5 Provision SSL certificate

```bash
curl -s -X POST "${BASE}/domains/${CUSTOMER_DOMAIN}/provision-ssl" \
  -H "X-API-Key: $API_KEY" | python3 -m json.tool
```

This runs certbot, issues a Let's Encrypt cert, writes the nginx config, and reloads nginx.

Expected response:
```json
{
  "message": "SSL certificate issued and nginx configured. Domain is now live!",
  "detail": "Certificate expires: 2025-04-15 ..."
}
```

### 6.6 Verify traffic is being proxied

```bash
# HTTPS should now work and proxy to the origin
curl -I https://www.alice-website.com

# Check nginx config
nginx -t
systemctl status nginx

# Check domain status via API
curl -s "${BASE}/domains/${CUSTOMER_DOMAIN}" \
  -H "X-API-Key: $API_KEY" | python3 -m json.tool
```

### 6.7 Verify unknown domains are rejected

```bash
# A domain NOT registered should return empty (nginx returns 444)
curl -v --connect-timeout 5 https://unknown-domain.com 2>&1 | grep -E "< HTTP|Empty|connected"
# Expected: connection closed or 444 / empty response
```

---

## 7. Load Testing & Error Rate Measurement

### 7.1 Install test dependencies

```bash
cd /opt/translation-proxy-src
pip install -r control_plane/requirements.txt
```

### 7.2 Run the pytest suite

```bash
PROXY_BASE_URL=http://localhost:8000 pytest tests/test_api.py -v --tb=short 2>&1 | tee /tmp/test_results.txt

# At the end you'll see error rates for concurrent tests:
# [ConcurrentHealthCheck] Requests=50  Errors=0  Error%=0.0%
# [ConcurrentDomainList]  Requests=30  Errors=0  Error%=0.0%
```

### 7.3 Run the Locust load test (headless)

```bash
# Create a test customer first
API_KEY=$(curl -s -X POST http://localhost:8000/customers \
  -H "Content-Type: application/json" \
  -d '{"email":"loadtest@example.com"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['api_key'])")

# Run load test: 50 concurrent users, 10 min
LOAD_TEST_API_KEY="$API_KEY" locust \
  -f tests/load_test.py \
  --host=http://localhost:8000 \
  --users 50 \
  --spawn-rate 10 \
  --run-time 5m \
  --headless \
  --csv /tmp/proxy_load 2>&1 | tail -30

# Check error rate
python3 -c "
import csv
with open('/tmp/proxy_load_stats.csv') as f:
    rows = list(csv.DictReader(f))
    total_row = [r for r in rows if r.get('Name') == 'Aggregated']
    if total_row:
        r = total_row[0]
        total = int(r['Request Count'])
        failures = int(r['Failure Count'])
        pct = failures/total*100 if total else 0
        print(f'Total: {total}  Failures: {failures}  Error%: {pct:.2f}%')
"
```

### 7.4 Benchmark nginx proxy throughput

Use `wrk` or `hey` to test the nginx proxy layer directly:

```bash
# Install wrk
apt-get install wrk -y

# Benchmark (replace with a real active domain)
wrk -t4 -c100 -d30s https://www.alice-website.com/

# Expected output:
# Requests/sec: ~5000-15000 (depending on hardware and origin response time)
# Latency (avg): < 5ms (local origin)
```

### 7.5 Expected error rates

| Test                    | Expected Error Rate |
|-------------------------|---------------------|
| /health (50 concurrent) | < 0.1%              |
| /domains GET (30 conc.) | < 0.1%              |
| Nginx proxy (100 conc.) | < 0.01%             |
| Certbot issuance        | < 1% (network dep.) |

---

## 8. Scaling Guide

### 8.1 Vertical scaling (single server)

Tune `/etc/nginx/nginx.conf`:
- `worker_processes auto;` → already uses all CPU cores
- `worker_connections 4096;` → increase to 8192 for very high traffic
- Enable `open_file_cache` for thousands of certs:

```nginx
open_file_cache max=10000 inactive=30s;
open_file_cache_valid 60s;
open_file_cache_min_uses 2;
open_file_cache_errors on;
```

Tune uvicorn workers:
```bash
# In systemd service file, increase workers to match CPU count
--workers $(nproc)
```

### 8.2 Horizontal scaling (multiple servers)

For multiple proxy servers:
1. Use a **shared PostgreSQL** (managed DB: RDS, Supabase, etc.)
2. Use **NFS or S3** to share `/etc/letsencrypt` across servers (or run certbot on one server and sync certs)
3. Use a **DNS load balancer** (Route53, Cloudflare) to spread traffic

### 8.3 Let's Encrypt rate limits

Let's Encrypt limits: 50 new certs per registered domain per week.
- For high-volume onboarding: use **ZeroSSL** or **Google Trust Services** (certbot supports them)
- Or use wildcard certs for your own domains
- For customer domains: LE rate limits are per-customer-domain, not your server

### 8.4 Nginx performance with 10,000+ domains

Nginx's `server_name` hash:

In `/etc/nginx/nginx.conf`, add inside `http {}`:
```nginx
server_names_hash_max_size   65536;
server_names_hash_bucket_size 128;
```

This ensures nginx can efficiently route 10k+ domain configs.

---

## 9. Monitoring & Maintenance

### 9.1 View logs

```bash
# Control plane logs
journalctl -u translation-proxy -f

# Nginx access/error logs
tail -f /var/log/nginx/access.log
tail -f /var/log/nginx/error.log
```

### 9.2 Check certificate expiry

```bash
# List all managed certs and their expiry
certbot certificates

# Check a specific domain
openssl x509 -enddate -noout -in /etc/letsencrypt/live/example.com/fullchain.pem
```

### 9.3 Force cert renewal (test)

```bash
certbot renew --dry-run
```

### 9.4 Monitor with the health endpoint

Set up an external monitor (e.g., UptimeRobot, Pingdom) on:
```
http://YOUR_SERVER_IP:8000/health
```

---

## 10. Troubleshooting

### nginx fails to start

```bash
nginx -t          # shows config errors
journalctl -u nginx -n 50
```

Common causes:
- Missing default cert → run `setup.sh` again
- Missing `dhparam.pem` → `openssl dhparam -out /etc/nginx/dhparam.pem 2048`
- Syntax error in a generated config → check `/etc/nginx/sites-enabled/`

### certbot fails

```bash
# Test with staging (no rate limits)
certbot certonly --webroot -w /var/www/acme-challenge \
    -d example.com --staging --agree-tos --email your@email.com
```

Common causes:
- Domain A record doesn't point to this server
- Port 80 is blocked by firewall → `ufw allow 80/tcp`
- `.well-known/acme-challenge/` not being served → check http-only config exists

### Control plane API returns 500

```bash
journalctl -u translation-proxy -n 100
# Look for Python tracebacks
```

### Database connection issues

```bash
sudo -u postgres psql -c "\l"
psql postgresql://proxy_user:proxy_pass@localhost/translation_proxy -c "SELECT count(*) FROM customers;"
```

### Check what nginx is actually doing for a domain

```bash
cat /etc/nginx/sites-enabled/example.com.conf
nginx -T | grep -A 30 "server_name example.com"
```
