import urllib.request
import logging
from typing import List

from pydantic import computed_field
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)


def _detect_public_ip() -> str:
    services = [
        "https://api.ipify.org",
        "https://ifconfig.me/ip",
        "https://icanhazip.com",
        "https://checkip.amazonaws.com",
    ]
    for url in services:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "translation-proxy/2.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                ip = resp.read().decode().strip()
                if ip and ip != "0.0.0.0":
                    logger.info("Auto-detected public IP: %s (from %s)", ip, url)
                    return ip
        except Exception:
            continue
    logger.warning("Could not auto-detect public IP — set OUR_SERVER_IP in .env")
    return "0.0.0.0"


class Settings(BaseSettings):
    # ── Database ──────────────────────────────────────────────────────────────
    DATABASE_URL: str = "postgresql://proxy_user:proxy_pass@localhost/translation_proxy"

    # ── Server ────────────────────────────────────────────────────────────────
    OUR_SERVER_IP:        str = ""
    CONTROL_PLANE_HOST:   str = "0.0.0.0"
    CONTROL_PLANE_PORT:   int = 8000

    # ── Security ──────────────────────────────────────────────────────────────
    SECRET_KEY:          str = ""
    # Comma-separated CORS origins: http://localhost:3000,https://dashboard.example.com
    ALLOWED_ORIGINS_STR: str = "http://localhost:3000,http://localhost:8080"

    @computed_field
    @property
    def ALLOWED_ORIGINS(self) -> List[str]:
        return [o.strip() for o in self.ALLOWED_ORIGINS_STR.split(",") if o.strip()]

    # ── Nginx ─────────────────────────────────────────────────────────────────
    NGINX_SITES_ENABLED:   str = "/etc/nginx/sites-enabled"
    NGINX_SITES_AVAILABLE: str = "/etc/nginx/sites-available"
    NGINX_ACME_WEBROOT:    str = "/var/www/acme-challenge"
    NGINX_SNIPPETS_DIR:    str = "/etc/nginx/snippets"

    # ── Certbot ───────────────────────────────────────────────────────────────
    CERTBOT_EMAIL:      str = "admin@example.com"
    CERTBOT_CONFIG_DIR: str = "/etc/letsencrypt"
    CERTBOT_WORK_DIR:   str = "/var/lib/letsencrypt"
    CERTBOT_LOGS_DIR:   str = "/var/log/letsencrypt"
    LETSENCRYPT_LIVE:   str = "/etc/letsencrypt/live"

    # ── App ───────────────────────────────────────────────────────────────────
    VERIFICATION_TXT_PREFIX: str   = "_proxy-verify"
    DNS_CHECK_TIMEOUT:        float = 5.0
    DNS_CHECK_RETRIES:        int   = 3

    # ── Translation ───────────────────────────────────────────────────────────
    DEEPL_API_KEY: str = ""
    TARGET_LANG:   str = "DE"
    WORKER_PORT:   int = 8001

    # ── CDN / S3 ──────────────────────────────────────────────────────────────
    CDN_BACKEND:         str = "db"
    CDN_BUCKET:          str = ""
    CDN_REGION:          str = "eu-central-1"
    CDN_ACCESS_KEY:      str = ""
    CDN_SECRET_KEY:      str = ""
    CDN_ENDPOINT_URL:    str = ""
    CDN_PUBLIC_BASE_URL: str = ""

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()

# Auto-detect public IP
if not settings.OUR_SERVER_IP or settings.OUR_SERVER_IP == "0.0.0.0":
    settings.OUR_SERVER_IP = _detect_public_ip()

# Warn if SECRET_KEY not set
if not settings.SECRET_KEY:
    import secrets
    settings.SECRET_KEY = secrets.token_urlsafe(32)
    logger.warning(
        "SECRET_KEY not set in .env — generated ephemeral key. "
        "Set SECRET_KEY in .env for session persistence across restarts."
    )
