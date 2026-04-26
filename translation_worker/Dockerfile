FROM python:3.12-slim

# lxml native deps
RUN apt-get update && apt-get install -y --no-install-recommends \
        libxml2 libxslt1.1 gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Deps first for layer cache
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY app/ ./app/

# Non-root user for security
RUN useradd -r -s /bin/false worker && chown -R worker /app
USER worker

EXPOSE 8001

CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8001", \
     "--workers", "4", \
     "--log-level", "info"]
