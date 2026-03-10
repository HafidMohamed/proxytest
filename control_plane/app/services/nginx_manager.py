"""
Nginx configuration manager.

Traffic flow (after provision-ssl):
  Visitor browser
    → DNS: cafezisimoukreuzau.de → Cloudflare edge IP  (must be orange cloud!)
    → Cloudflare edge (adds CF-Connecting-IP: real visitor IP)
    → Our server port 443 (from a Cloudflare IP → UFW allows it)
    → Nginx vhost for cafezisimoukreuzau.de
    → proxy_pass to customer's backend_url (e.g. https://origin.com)
    → Backend returns response
    → Nginx forwards response to Cloudflare
    → Cloudflare forwards to visitor browser

CRITICAL: The domain's A record in Cloudflare DNS MUST be set to
"Proxied" (orange cloud icon), NOT "DNS only" (grey cloud).
If it is grey cloud:
  - Visitor hits our server IP directly
  - UFW drops port 443 (only CF IPs are allowed)
  - Browser shows "connection refused" or timeout
"""

import logging
import os
import subprocess
from pathlib import Path
from typing import Tuple

from ..config import settings

logger = logging.getLogger(__name__)

# ── Templates ─────────────────────────────────────────────────────────────────

# Used BEFORE SSL cert is issued — serves ACME challenge on port 80
_HTTP_ONLY_CONF = """\
# HTTP-only vhost for {domain} (pre-SSL)
server {{
    listen 80;
    server_name {domain};

    # ACME HTTP-01 challenge — port 80 must be open to ALL IPs for this
    location /.well-known/acme-challenge/ {{
        root {acme_webroot};
        try_files $uri =404;
        allow all;
    }}

    location / {{
        return 301 https://$host$request_uri;
    }}
}}
"""

# Full production config — used AFTER SSL cert is issued
_SSL_CONF = """\
# Reverse-proxy config for {domain}
# Auto-generated — do not edit manually

# ── HTTP: ACME challenge + redirect to HTTPS ─────────────────────────────────
server {{
    listen 80;
    server_name {domain};

    location /.well-known/acme-challenge/ {{
        root {acme_webroot};
        try_files $uri =404;
        allow all;
    }}

    location / {{
        return 301 https://$host$request_uri;
    }}
}}

# ── HTTPS: full reverse proxy ─────────────────────────────────────────────────
server {{
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name {domain};

    # TLS certificate (issued by Let's Encrypt via provision-ssl)
    ssl_certificate     {ssl_cert};
    ssl_certificate_key {ssl_key};
    include             {snippets_dir}/ssl-params.conf;

    # Only allow connections from Cloudflare IPs on port 443.
    # Non-CF connections are also blocked by UFW at kernel level.
    # NOTE: visitor's browser must go through Cloudflare (orange cloud in DNS).
    include {snippets_dir}/cloudflare-allow.conf;

    # Unwrap real visitor IP from CF-Connecting-IP header.
    # cloudflare-realip.conf is also included globally in nginx.conf
    # but per-vhost inclusion ensures it applies even if global include is missing.
    # nginx deduplicates set_real_ip_from directives automatically.
    include {snippets_dir}/cloudflare-realip.conf;

    # Security headers
    add_header Strict-Transport-Security "max-age=63072000; includeSubDomains; preload" always;
    add_header X-Content-Type-Options    "nosniff" always;
    add_header X-Frame-Options           "SAMEORIGIN" always;
    add_header Referrer-Policy           "strict-origin-when-cross-origin" always;

    # ── Proxy ALL requests to customer's backend ──────────────────────────────
    location / {{
        # Forward to customer's origin server stored in database
        proxy_pass {backend_url};

        proxy_http_version 1.1;

        # WebSocket support
        proxy_set_header Upgrade    $http_upgrade;
        proxy_set_header Connection $connection_upgrade;

        # Send backend's own hostname as Host so it accepts the request.
        # $proxy_host = hostname extracted from proxy_pass URL.
        # e.g. proxy_pass https://origin.example.com → Host: origin.example.com
        # e.g. proxy_pass https://1.2.3.4            → Host: 1.2.3.4
        proxy_set_header Host $proxy_host;

        # Tell backend who the real visitor is
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Original-Host   $host;

        # Forward Cloudflare metadata to backend (useful for geo-blocking, bot detection)
        proxy_set_header CF-Connecting-IP $http_cf_connecting_ip;
        proxy_set_header CF-IPCountry     $http_cf_ipcountry;
        proxy_set_header CF-Ray           $http_cf_ray;

        # Don't reveal backend server identity
        proxy_hide_header X-Powered-By;
        proxy_hide_header Server;

        # TLS to HTTPS backends
        # proxy_ssl_server_name on  → sends correct SNI for hostname-based backends
        # proxy_ssl_verify off      → accepts self-signed certs on private origins
        proxy_ssl_server_name on;
        proxy_ssl_verify      off;

        # Timeouts — increase proxy_read_timeout for slow backends
        proxy_connect_timeout 15s;
        proxy_send_timeout    60s;
        proxy_read_timeout    60s;

        # Response buffering — improves performance under load
        proxy_buffering         on;
        proxy_buffer_size       16k;
        proxy_buffers          16 16k;
        proxy_busy_buffers_size 32k;

        # DO NOT set proxy_intercept_errors — it intercepts backend redirects
        # and 404s which breaks most websites. Let backend responses pass through.
    }}
}}
"""

_SSL_PARAMS = """\
# Shared TLS settings — auto-generated
ssl_protocols TLSv1.2 TLSv1.3;
ssl_prefer_server_ciphers off;
ssl_dhparam /etc/nginx/dhparam.pem;
ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305:DHE-RSA-AES128-GCM-SHA256;
ssl_session_cache   shared:SSL:10m;
ssl_session_timeout 1d;
ssl_session_tickets off;
ssl_stapling        on;
ssl_stapling_verify on;
resolver 8.8.8.8 1.1.1.1 valid=300s;
resolver_timeout 5s;
"""


# ── Internal helpers ──────────────────────────────────────────────────────────

def _run(cmd: list[str], timeout: int = 30) -> Tuple[int, str, str]:
    result = subprocess.run(
        ["sudo"] + cmd, capture_output=True, text=True, timeout=timeout
    )
    return result.returncode, result.stdout, result.stderr


def _available_path(domain: str, suffix: str = "") -> Path:
    return Path(settings.NGINX_SITES_AVAILABLE) / f"{domain}{suffix}.conf"


def _enabled_path(domain: str, suffix: str = "") -> Path:
    return Path(settings.NGINX_SITES_ENABLED) / f"{domain}{suffix}.conf"


def _enable(domain: str, suffix: str = "") -> None:
    avail = _available_path(domain, suffix)
    enbld = _enabled_path(domain, suffix)
    if enbld.exists() or enbld.is_symlink():
        enbld.unlink()
    # Use absolute path for symlink target — avoids breakage if cwd changes
    enbld.symlink_to(avail.resolve())


def _disable(domain: str, suffix: str = "") -> None:
    enbld = _enabled_path(domain, suffix)
    if enbld.exists() or enbld.is_symlink():
        enbld.unlink()


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


# ── Public API ────────────────────────────────────────────────────────────────

class NginxManager:

    def __init__(self):
        self._ensure_dirs()
        self._write_ssl_params()

    def _ensure_dirs(self):
        for d in [
            settings.NGINX_SITES_AVAILABLE,
            settings.NGINX_SITES_ENABLED,
            settings.NGINX_SNIPPETS_DIR,
            settings.NGINX_ACME_WEBROOT,
        ]:
            os.makedirs(d, exist_ok=True)

    def _write_ssl_params(self):
        p = Path(settings.NGINX_SNIPPETS_DIR) / "ssl-params.conf"
        if not p.exists():
            _write(p, _SSL_PARAMS)

    # ── Config writers ────────────────────────────────────────────────────────

    def write_http_only_config(self, domain: str) -> str:
        """Write HTTP-only vhost for ACME challenge before SSL is issued."""
        content = _HTTP_ONLY_CONF.format(
            domain=domain,
            acme_webroot=settings.NGINX_ACME_WEBROOT,
        )
        path = _available_path(domain, ".http")
        _write(path, content)
        _enable(domain, ".http")
        logger.info("HTTP-only config written: %s", domain)
        return str(path)

    def remove_http_only_config(self, domain: str) -> None:
        _disable(domain, ".http")
        p = _available_path(domain, ".http")
        if p.exists():
            p.unlink()

    def write_ssl_config(
        self, domain: str, backend_url: str, ssl_cert: str, ssl_key: str
    ) -> str:
        """
        Write the full HTTPS reverse-proxy vhost for a customer domain.
        Proxies ALL requests → customer's backend_url.
        """
        # Normalise backend_url: strip trailing slash to avoid double-slash
        backend_url = backend_url.rstrip("/")

        content = _SSL_CONF.format(
            domain=domain,
            backend_url=backend_url,
            ssl_cert=ssl_cert,
            ssl_key=ssl_key,
            acme_webroot=settings.NGINX_ACME_WEBROOT,
            snippets_dir=settings.NGINX_SNIPPETS_DIR,
        )
        path = _available_path(domain)
        _write(path, content)
        _enable(domain)
        self.remove_http_only_config(domain)
        logger.info("SSL proxy config written: %s → %s", domain, backend_url)
        return str(path)

    def remove_domain_config(self, domain: str) -> None:
        _disable(domain)
        _disable(domain, ".http")
        for p in [_available_path(domain), _available_path(domain, ".http")]:
            if p.exists():
                p.unlink()
        logger.info("Config removed: %s", domain)

    # ── Nginx control ─────────────────────────────────────────────────────────

    def test_config(self) -> Tuple[bool, str]:
        rc, out, err = _run(["nginx", "-t"])
        msg = (out + err).strip()
        if rc != 0:
            logger.error("nginx -t FAILED: %s", msg)
        return rc == 0, msg

    def reload(self) -> Tuple[bool, str]:
        ok, msg = self.test_config()
        if not ok:
            return False, f"nginx config test failed: {msg}"
        rc, out, err = _run(["nginx", "-s", "reload"])
        if rc == 0:
            logger.info("nginx reloaded OK")
            return True, "nginx reloaded"
        return False, f"nginx reload failed: {(out+err).strip()}"

    def safe_write_and_reload(
        self, domain: str, backend_url: str, ssl_cert: str, ssl_key: str
    ) -> Tuple[bool, str, str]:
        """Write config + reload. Auto-rollback on failure."""
        config_path = self.write_ssl_config(domain, backend_url, ssl_cert, ssl_key)
        ok, msg = self.reload()
        if not ok:
            self.remove_domain_config(domain)
            self.reload()
            return False, "", msg
        return True, config_path, msg

    # ── Diagnostics ───────────────────────────────────────────────────────────

    def get_domain_config(self, domain: str) -> str | None:
        """Return the nginx config content for a domain, or None if not found."""
        p = _available_path(domain)
        return p.read_text() if p.exists() else None

    def list_active_domains(self) -> list[str]:
        enabled = Path(settings.NGINX_SITES_ENABLED)
        if not enabled.exists():
            return []
        return [
            p.stem for p in enabled.iterdir()
            if p.name.endswith(".conf") and not p.name.endswith(".http.conf")
        ]

    def is_nginx_running(self) -> bool:
        rc, _, _ = _run(["pgrep", "-x", "nginx"])
        return rc == 0
