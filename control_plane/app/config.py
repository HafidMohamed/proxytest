import secrets
import urllib.request
import logging

from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)


def _detect_public_ip() -> str:
    """
    Try several public IP detection services in order.
    Falls back to 0.0.0.0 only if all fail.
    """
    services = [
        "https://api.ipify.org",
        "https://ifconfig.me/ip",
        "https://icanhazip.com",
        "https://checkip.amazonaws.com",
    ]
    for url in services:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "translation-proxy/1.0"})
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

    # ── Our server ────────────────────────────────────────────────────────────
    # Set OUR_SERVER_IP in .env to pin it. If unset, auto-detected at startup.
    OUR_SERVER_IP: str = ""
    CONTROL_PLANE_HOST: str = "0.0.0.0"
    CONTROL_PLANE_PORT: int = 8000

    # ── Nginx paths ───────────────────────────────────────────────────────────
    NGINX_SITES_ENABLED: str   = "/etc/nginx/sites-enabled"
    NGINX_SITES_AVAILABLE: str = "/etc/nginx/sites-available"
    NGINX_ACME_WEBROOT: str    = "/var/www/acme-challenge"
    NGINX_SNIPPETS_DIR: str    = "/etc/nginx/snippets"

    # ── Certbot / Let's Encrypt ───────────────────────────────────────────────
    CERTBOT_EMAIL: str      = "admin@example.com"
    CERTBOT_CONFIG_DIR: str = "/etc/letsencrypt"
    CERTBOT_WORK_DIR: str   = "/var/lib/letsencrypt"
    CERTBOT_LOGS_DIR: str   = "/var/log/letsencrypt"
    LETSENCRYPT_LIVE: str   = "/etc/letsencrypt/live"

    # ── Application ───────────────────────────────────────────────────────────
    SECRET_KEY: str              = secrets.token_urlsafe(32)
    VERIFICATION_TXT_PREFIX: str = "_proxy-verify"
    DNS_CHECK_TIMEOUT: float     = 5.0
    DNS_CHECK_RETRIES: int       = 3

    class Config:
        env_file = ".env"


settings = Settings()

# Auto-detect public IP if not explicitly set in .env
if not settings.OUR_SERVER_IP or settings.OUR_SERVER_IP == "0.0.0.0":
    settings.OUR_SERVER_IP = _detect_public_ip()
