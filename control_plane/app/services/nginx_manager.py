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
"""

import logging
import subprocess
from pathlib import Path
from typing import Tuple

from ..config import settings

logger = logging.getLogger(__name__)

# ── Templates ─────────────────────────────────────────────────────────────────

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

    ssl_certificate     {ssl_cert};
    ssl_certificate_key {ssl_key};
    include             {snippets_dir}/ssl-params.conf;

    # Only allow Cloudflare IPs — blocks direct-to-origin attacks
    include {snippets_dir}/cloudflare-allow.conf;

    # Unwrap real visitor IP from CF-Connecting-IP header
    include {snippets_dir}/cloudflare-realip.conf;

    add_header Strict-Transport-Security "max-age=63072000; includeSubDomains; preload" always;
    add_header X-Content-Type-Options    "nosniff" always;
    add_header X-Frame-Options           "SAMEORIGIN" always;
    add_header Referrer-Policy           "strict-origin-when-cross-origin" always;

    # ── Proxy ALL requests to customer's backend ──────────────────────────────
    location / {{
        proxy_pass {backend_url};

        proxy_http_version 1.1;

        # WebSocket support
        proxy_set_header Upgrade    $http_upgrade;
        proxy_set_header Connection $connection_upgrade;

        # Use the backend's hostname as the Host header
        proxy_set_header Host $proxy_host;

        # Tell backend the real visitor IP (unwrapped from CF-Connecting-IP)
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Original-Host   $host;

        # Forward Cloudflare metadata to backend
        proxy_set_header CF-Connecting-IP $http_cf_connecting_ip;
        proxy_set_header CF-IPCountry     $http_cf_ipcountry;
        proxy_set_header CF-Ray           $http_cf_ray;

        proxy_hide_header X-Powered-By;
        proxy_hide_header Server;

        # Accept self-signed certs on private/IP origins
        proxy_ssl_server_name on;
        proxy_ssl_verify      off;

        proxy_connect_timeout 15s;
        proxy_send_timeout    60s;
        proxy_read_timeout    60s;

        proxy_buffering         on;
        proxy_buffer_size       16k;
        proxy_buffers          16 16k;
        proxy_busy_buffers_size 32k;
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

def _run(cmd: list, timeout: int = 30) -> Tuple[int, str, str]:
    """
    Run a command via sudo with full exception handling.

    BUG FIX: Original had no try/except — FileNotFoundError (command not
    found) or TimeoutExpired would propagate unhandled and could kill the
    uvicorn worker, producing a blank HTTP response instead of a JSON error.
    """
    full_cmd = ["sudo"] + [str(c) for c in cmd]
    logger.info("Running: %s", " ".join(full_cmd))
    try:
        result = subprocess.run(
            full_cmd, capture_output=True, text=True, timeout=timeout
        )
        if result.returncode != 0:
            logger.warning(
                "Command failed (%d):\nSTDOUT: %s\nSTDERR: %s",
                result.returncode, result.stdout, result.stderr,
            )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        logger.error("Command timed out after %ds: %s", timeout, " ".join(full_cmd))
        return 1, "", f"Command timed out after {timeout}s"
    except FileNotFoundError as exc:
        logger.error("Command not found: %s — is it installed?", full_cmd[1])
        return 1, "", f"Command not found: {full_cmd[1]}: {exc}"
    except Exception as exc:
        logger.exception("Unexpected error running %s", full_cmd)
        return 1, "", f"Unexpected error: {exc}"


def _write(path: Path, content: str) -> None:
    """
    Write file content using sudo tee (handles root-owned directories).

    BUG FIX: Original used path.write_text() directly, which fails when the
    app runs as www-data because /etc/nginx/sites-available/ and
    /etc/nginx/sites-enabled/ are owned by root.  We now mirror the pattern
    used in cloudflare_manager.py — pipe content through 'sudo tee' so the
    write succeeds regardless of directory ownership.
    """
    # Ensure parent directory exists
    rc, _, err = _run(["mkdir", "-p", str(path.parent)], timeout=10)
    if rc != 0:
        raise RuntimeError(f"mkdir -p {path.parent} failed: {err}")

    result = subprocess.run(
        ["sudo", "tee", str(path)],
        input=content,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to write {path}: {result.stderr}")


def _available_path(domain: str, suffix: str = "") -> Path:
    return Path(settings.NGINX_SITES_AVAILABLE) / f"{domain}{suffix}.conf"


def _enabled_path(domain: str, suffix: str = "") -> Path:
    return Path(settings.NGINX_SITES_ENABLED) / f"{domain}{suffix}.conf"


def _enable(domain: str, suffix: str = "") -> None:
    """
    Create symlink sites-enabled → sites-available using sudo ln.

    BUG FIX: Original used enbld.symlink_to() directly, which fails for
    www-data because /etc/nginx/sites-enabled/ is root-owned.
    """
    avail = _available_path(domain, suffix)
    enbld = _enabled_path(domain, suffix)
    # Remove stale symlink first
    _run(["rm", "-f", str(enbld)], timeout=10)
    # Create new symlink (use absolute resolved path as target)
    rc, _, err = _run(["ln", "-sf", str(avail.resolve()), str(enbld)], timeout=10)
    if rc != 0:
        raise RuntimeError(f"Failed to enable {domain}{suffix}: {err}")


def _disable(domain: str, suffix: str = "") -> None:
    """
    Remove sites-enabled symlink using sudo rm.

    BUG FIX: Original used enbld.unlink() directly — fails for www-data.
    """
    enbld = _enabled_path(domain, suffix)
    _run(["rm", "-f", str(enbld)], timeout=10)


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
            try:
                Path(d).mkdir(parents=True, exist_ok=True)
            except PermissionError:
                _run(["mkdir", "-p", d], timeout=10)

    def _write_ssl_params(self):
        p = Path(settings.NGINX_SNIPPETS_DIR) / "ssl-params.conf"
        if not p.exists():
            try:
                _write(p, _SSL_PARAMS)
            except Exception as exc:
                logger.warning("Could not write ssl-params.conf: %s", exc)

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
        _run(["rm", "-f", str(_available_path(domain, ".http"))], timeout=10)

    def write_ssl_config(
        self, domain: str, backend_url: str, ssl_cert: str, ssl_key: str
    ) -> str:
        """
        Write the full HTTPS reverse-proxy vhost for a customer domain.
        Proxies ALL requests → customer's backend_url.
        """
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
        _run(["rm", "-f", str(_available_path(domain))], timeout=10)
        _run(["rm", "-f", str(_available_path(domain, ".http"))], timeout=10)
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
        p = _available_path(domain)
        try:
            return p.read_text() if p.exists() else None
        except PermissionError:
            return None

    def list_active_domains(self) -> list:
        enabled = Path(settings.NGINX_SITES_ENABLED)
        if not enabled.exists():
            return []
        try:
            return [
                p.stem for p in enabled.iterdir()
                if p.name.endswith(".conf") and not p.name.endswith(".http.conf")
            ]
        except PermissionError:
            return []

    def is_nginx_running(self) -> bool:
        rc, _, _ = _run(["pgrep", "-x", "nginx"])
        return rc == 0
