FROM python:3.12-slim

# System deps: curl (healthcheck), libpq (postgres), nginx, certbot
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl libpq-dev gcc \
        nginx certbot python3-certbot-nginx \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (layer cache — only rebuilds when requirements.txt changes)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app/ ./control_plane/app/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Pre-create nginx directories
RUN mkdir -p \
        /etc/nginx/sites-available \
        /etc/nginx/sites-enabled \
        /etc/nginx/snippets \
        /var/www/acme-challenge \
        /var/log/nginx \
        /run \
    && rm -f /etc/nginx/sites-enabled/default

EXPOSE 80 8000

CMD ["/entrypoint.sh"]

# Ensure control_plane package is importable
RUN touch /app/control_plane/__init__.py
