import secrets
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── Database ──────────────────────────────────────────────────────────────
    DATABASE_URL: str = "postgresql://proxy_user:proxy_pass@localhost/translation_proxy"

    # ── Our server ────────────────────────────────────────────────────────────
    OUR_SERVER_IP: str = "0.0.0.0"
    CONTROL_PLANE_HOST: str = "0.0.0.0"
    CONTROL_PLANE_PORT: int = 8000

    # ── Nginx paths ───────────────────────────────────────────────────────────
    NGINX_SITES_ENABLED: str = "/etc/nginx/sites-enabled"
    NGINX_SITES_AVAILABLE: str = "/etc/nginx/sites-available"
    NGINX_ACME_WEBROOT: str = "/var/www/acme-challenge"
    NGINX_SNIPPETS_DIR: str = "/etc/nginx/snippets"

    # ── Certbot / Let's Encrypt ───────────────────────────────────────────────
    CERTBOT_EMAIL: str = "admin@example.com"
    # These three dirs must be writable by root (certbot runs via sudo)
    CERTBOT_CONFIG_DIR: str = "/etc/letsencrypt"
    CERTBOT_WORK_DIR: str   = "/var/lib/letsencrypt"
    CERTBOT_LOGS_DIR: str   = "/var/log/letsencrypt"
    # Derived – kept for backward compat
    LETSENCRYPT_LIVE: str   = "/etc/letsencrypt/live"

    # ── Application ───────────────────────────────────────────────────────────
    SECRET_KEY: str = secrets.token_urlsafe(32)
    VERIFICATION_TXT_PREFIX: str = "_proxy-verify"
    DNS_CHECK_TIMEOUT: float = 5.0
    DNS_CHECK_RETRIES: int = 3

    class Config:
        env_file = ".env"


settings = Settings()
