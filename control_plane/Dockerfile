FROM python:3.12-slim
WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl libpq-dev nginx certbot \
    && rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY . ./control_plane/

# Pre-create nginx dirs
RUN mkdir -p /etc/nginx/sites-available \
             /etc/nginx/sites-enabled \
             /etc/nginx/snippets \
             /var/www/acme-challenge \
             /var/log/nginx \
             /tmp/letsencrypt/live \
    && rm -f /etc/nginx/sites-enabled/default

# Copy entrypoint
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 80 8000

CMD ["/entrypoint.sh"]
