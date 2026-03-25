#!/bin/bash
set -e

echo "[entrypoint] Starting nginx..."

# Create required dirs
mkdir -p /etc/nginx/sites-available \
         /etc/nginx/sites-enabled \
         /etc/nginx/snippets \
         /var/www/acme-challenge \
         /var/log/nginx \
         /run

# Remove default nginx site that conflicts on port 80
rm -f /etc/nginx/sites-enabled/default

# Write a minimal default nginx config if none exists
if [ ! -f /etc/nginx/sites-enabled/default-proxy ]; then
cat > /etc/nginx/sites-available/default-proxy.conf << 'EOF'
server {
    listen 80 default_server;
    server_name _;

    location /.well-known/acme-challenge/ {
        root /var/www/acme-challenge;
        try_files $uri =404;
        allow all;
    }

    location / {
        return 444;
    }
}
EOF
ln -sf /etc/nginx/sites-available/default-proxy.conf \
       /etc/nginx/sites-enabled/default-proxy.conf
fi

# Test nginx config
nginx -t

# Start nginx in background
nginx

echo "[entrypoint] nginx started (PID: $(cat /run/nginx.pid))"
echo "[entrypoint] Starting uvicorn..."

exec uvicorn control_plane.app.main:app \
    --host 0.0.0.0 \
    --port 8000 \
    --workers 2 \
    --log-level info
