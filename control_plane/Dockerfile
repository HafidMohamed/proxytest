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

# Pre-create nginx dirs for dev
RUN mkdir -p /tmp/nginx/{sites-available,sites-enabled,snippets,acme} \
             /tmp/letsencrypt/live

EXPOSE 8000

CMD ["uvicorn", "control_plane.app.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "2", "--log-level", "info"]
