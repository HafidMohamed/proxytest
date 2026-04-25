"""
Nginx Configuration Manager  (v2)
===================================

New in v2:
  - Subdirectory routing: /de/, /fr/ etc. served from the same domain
  - Subdomain routing:    de.example.com, fr.example.com  (existing behavior, now explicit)
  - Per-language location blocks generated automatically from TranslationConfig.languages
  - Language switcher + hreflang handled by seo_crawler; nginx just routes correctly

Subdirectory mode (routing_mode=SUBDIRECTORY):
  One vhost handles all languages + origin.
  /de/* → worker with X-Translate-Lang: DE
  /fr/* → worker with X-Translate-Lang: FR
  /     → origin (no translation)

Subdomain mode (routing_mode=SUBDOMAIN):
  Original vhost: example.com → origin (unchanged)
  Per-language vhosts: de.example.com → worker with X-Translate-Lang: DE
  Customer must add CNAME de.example.com → our server for each language.
"""

import logging
import subprocess
from pathlib import Path
from typing import Tuple, List

from ..config import settings

logger = logging.getLogger(__name__)


# ── Templates ──────────────────────────────────────────────────────────────

_HTTP_ONLY_CONF = """\
# HTTP-only vhost for {domain} (pre-SSL)
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
"""

# ── Subdirectory mode: all languages on one domain ─────────────────────────
# /de/path → translation worker with lang=DE
# /        → straight proxy to origin (no translation)

_SSL_SUBDIRECTORY_CONF = """\
# Reverse-proxy config for {domain} (subdirectory routing)
# Languages: {languages}
# Auto-generated — do not edit manually

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

server {{
    listen 443 ssl;
    http2 on;
    listen [::]:443 ssl;
    server_name {domain};

    ssl_certificate     {ssl_cert};
    ssl_certificate_key {ssl_key};
    include             {snippets_dir}/ssl-params.conf;

    add_header Strict-Transport-Security "max-age=63072000; includeSubDomains; preload" always;
    add_header X-Content-Type-Options    "nosniff" always;
    add_header X-Frame-Options           "SAMEORIGIN" always;
    add_header Referrer-Policy           "strict-origin-when-cross-origin" always;

{lang_locations}

    # Origin passthrough (no translation)
    location / {{
        proxy_pass {backend_url};
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Original-Host   $host;
        proxy_set_header Upgrade    $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_hide_header X-Powered-By;
        proxy_hide_header Server;
        proxy_connect_timeout 15s;
        proxy_send_timeout    60s;
        proxy_read_timeout    60s;
    }}
}}
"""

_LANG_LOCATION_BLOCK = """\
    # Language: {lang}
    location /{lang_lower}/ {{
        # Strip the language prefix before forwarding to origin via worker
        rewrite ^/{lang_lower}(/.*)$ $1 break;
        set $upstream_url {backend_url}$uri$is_args$args;

        proxy_pass http://127.0.0.1:{worker_port};
        proxy_http_version 1.1;

        proxy_set_header X-Upstream-URL   $upstream_url;
        proxy_set_header X-Translate-Lang {lang};
        proxy_set_header X-Original-Host  $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade    $http_upgrade;
        proxy_set_header Connection $connection_upgrade;

        proxy_hide_header X-Powered-By;
        proxy_hide_header Server;

        proxy_connect_timeout 15s;
        proxy_send_timeout    60s;
        proxy_read_timeout    60s;
        proxy_buffering         on;
        proxy_buffer_size       16k;
        proxy_buffers          16 16k;
        proxy_busy_buffers_size 32k;
    }}
"""

# ── Subdomain mode: de.example.com ────────────────────────────────────────

_SSL_SUBDOMAIN_CONF = """\
# Reverse-proxy config for {subdomain}.{domain} (subdomain routing, lang={lang})
# Auto-generated — do not edit manually

server {{
    listen 80;
    server_name {subdomain}.{domain};

    location /.well-known/acme-challenge/ {{
        root {acme_webroot};
        try_files $uri =404;
        allow all;
    }}

    location / {{
        return 301 https://$host$request_uri;
    }}
}}

server {{
    listen 443 ssl;
    http2 on;
    listen [::]:443 ssl;
    server_name {subdomain}.{domain};

    ssl_certificate     {ssl_cert};
    ssl_certificate_key {ssl_key};
    include             {snippets_dir}/ssl-params.conf;

    add_header Strict-Transport-Security "max-age=63072000; includeSubDomains; preload" always;
    add_header X-Content-Type-Options    "nosniff" always;

    location / {{
        set $upstream_url {backend_url}$request_uri;

        proxy_pass http://127.0.0.1:{worker_port};
        proxy_http_version 1.1;

        proxy_set_header X-Upstream-URL   $upstream_url;
        proxy_set_header X-Translate-Lang {lang};
        proxy_set_header X-Original-Host  $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade    $http_upgrade;
        proxy_set_header Connection $connection_upgrade;

        proxy_hide_header X-Powered-By;
        proxy_hide_header Server;

        proxy_connect_timeout 15s;
        proxy_send_timeout    60s;
        proxy_read_timeout    60s;
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


# ── Helpers ────────────────────────────────────────────────────────────────

def _run(cmd: list, timeout: int = 30) -> Tuple[int, str, str]:
    import os
    full_cmd = (["sudo"] + [str(c) for c in cmd]) if os.getuid() != 0 else [str(c) for c in cmd]
    logger.info("Running: %s", " ".join(full_cmd))
    try:
        result = subprocess.run(full_cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            logger.warning("Command failed (%d):\nSTDOUT: %s\nSTDERR: %s",
                           result.returncode, result.stdout, result.stderr)
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return 1, "", f"Command timed out after {timeout}s"
    except FileNotFoundError as exc:
        return 1, "", f"Command not found: {exc}"
    except Exception as exc:
        return 1, "", f"Unexpected error: {exc}"


def _write(path: Path, content: str) -> None:
    import os
    _run(["mkdir", "-p", str(path.parent)], timeout=10)
    result = subprocess.run(
        (["sudo", "tee", str(path)] if os.getuid() != 0 else ["tee", str(path)]),
        input=content, capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to write {path}: {result.stderr}")


def _available_path(name: str, suffix: str = "") -> Path:
    return Path(settings.NGINX_SITES_AVAILABLE) / f"{name}{suffix}.conf"


def _enabled_path(name: str, suffix: str = "") -> Path:
    return Path(settings.NGINX_SITES_ENABLED) / f"{name}{suffix}.conf"


def _enable(name: str, suffix: str = "") -> None:
    avail = _available_path(name, suffix)
    enbld = _enabled_path(name, suffix)
    _run(["rm", "-f", str(enbld)], timeout=10)
    rc, _, err = _run(["ln", "-sf", str(avail.resolve()), str(enbld)], timeout=10)
    if rc != 0:
        raise RuntimeError(f"Failed to enable {name}{suffix}: {err}")


def _disable(name: str, suffix: str = "") -> None:
    _run(["rm", "-f", str(_enabled_path(name, suffix))], timeout=10)


# ── Public API ─────────────────────────────────────────────────────────────

class NginxManager:

    def __init__(self):
        self._ensure_dirs()
        self._write_ssl_params()

    def _ensure_dirs(self):
        for d in [settings.NGINX_SITES_AVAILABLE, settings.NGINX_SITES_ENABLED,
                  settings.NGINX_SNIPPETS_DIR, settings.NGINX_ACME_WEBROOT]:
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

    # ── HTTP-only (pre-SSL) ────────────────────────────────────────────────

    def write_http_only_config(self, domain: str) -> str:
        content = _HTTP_ONLY_CONF.format(
            domain=domain, acme_webroot=settings.NGINX_ACME_WEBROOT)
        path = _available_path(domain, ".http")
        _write(path, content)
        _enable(domain, ".http")
        return str(path)

    def remove_http_only_config(self, domain: str) -> None:
        _disable(domain, ".http")
        _run(["rm", "-f", str(_available_path(domain, ".http"))], timeout=10)

    # ── Subdirectory routing (default) ────────────────────────────────────

    def write_subdirectory_config(
        self,
        domain: str,
        backend_url: str,
        ssl_cert: str,
        ssl_key: str,
        languages: List[str],
        worker_port: int = None,
    ) -> str:
        """
        Single vhost handles all language prefixes.
        /de/* → worker(lang=DE) → origin
        /fr/* → worker(lang=FR) → origin
        /     → origin (direct)
        """
        backend_url = backend_url.rstrip("/")
        wp = worker_port or getattr(settings, "WORKER_PORT", 8001)

        lang_locations = ""
        for lang in languages:
            lang_locations += _LANG_LOCATION_BLOCK.format(
                lang=lang.upper(),
                lang_lower=lang.lower(),
                backend_url=backend_url,
                worker_port=wp,
            )

        content = _SSL_SUBDIRECTORY_CONF.format(
            domain=domain,
            languages=",".join(languages),
            backend_url=backend_url,
            ssl_cert=ssl_cert,
            ssl_key=ssl_key,
            acme_webroot=settings.NGINX_ACME_WEBROOT,
            snippets_dir=settings.NGINX_SNIPPETS_DIR,
            lang_locations=lang_locations,
        )
        avail = _available_path(domain)
        _write(avail, content)
        _enable(domain)
        self.remove_http_only_config(domain)
        logger.info("Subdirectory config written: %s langs=%s", domain, languages)
        return str(avail)

    # ── Subdomain routing ─────────────────────────────────────────────────

    def write_subdomain_configs(
        self,
        domain: str,
        backend_url: str,
        ssl_cert: str,
        ssl_key: str,
        languages: List[str],
        worker_port: int = None,
    ) -> List[str]:
        """
        One vhost per language subdomain: de.domain, fr.domain, etc.
        Caller is responsible for obtaining wildcard SSL cert or per-subdomain certs.
        """
        backend_url = backend_url.rstrip("/")
        wp = worker_port or getattr(settings, "WORKER_PORT", 8001)
        paths = []
        for lang in languages:
            subdomain = lang.lower()
            name = f"{subdomain}.{domain}"
            content = _SSL_SUBDOMAIN_CONF.format(
                domain=domain,
                subdomain=subdomain,
                lang=lang.upper(),
                backend_url=backend_url,
                ssl_cert=ssl_cert,
                ssl_key=ssl_key,
                acme_webroot=settings.NGINX_ACME_WEBROOT,
                snippets_dir=settings.NGINX_SNIPPETS_DIR,
                worker_port=wp,
            )
            avail = _available_path(name)
            _write(avail, content)
            _enable(name)
            paths.append(str(avail))
            logger.info("Subdomain config written: %s (lang=%s)", name, lang)
        return paths

    # ── Legacy single-lang SSL config (backwards compat) ─────────────────

    def write_ssl_config(self, domain, backend_url, ssl_cert, ssl_key,
                         worker_port=None, target_lang=None):
        """Backwards-compatible single-language config."""
        lang = target_lang or getattr(settings, "TARGET_LANG", "DE")
        return self.write_subdirectory_config(
            domain, backend_url, ssl_cert, ssl_key,
            languages=[lang], worker_port=worker_port,
        )

    # ── Update languages for existing domain ──────────────────────────────

    def update_languages(
        self,
        domain: str,
        backend_url: str,
        ssl_cert: str,
        ssl_key: str,
        languages: List[str],
        routing_mode: str = "subdirectory",
        worker_port: int = None,
    ) -> Tuple[bool, str]:
        """Regenerate nginx config when languages list changes."""
        try:
            if routing_mode == "subdomain":
                self.write_subdomain_configs(domain, backend_url, ssl_cert, ssl_key,
                                             languages, worker_port)
            else:
                self.write_subdirectory_config(domain, backend_url, ssl_cert, ssl_key,
                                               languages, worker_port)
            ok, msg = self.reload()
            return ok, msg
        except Exception as exc:
            return False, str(exc)

    def remove_domain_config(self, domain: str) -> None:
        _disable(domain)
        _disable(domain, ".http")
        _run(["rm", "-f", str(_available_path(domain))], timeout=10)
        _run(["rm", "-f", str(_available_path(domain, ".http"))], timeout=10)
        logger.info("Config removed: %s", domain)

    # ── Nginx control ──────────────────────────────────────────────────────

    def test_config(self) -> Tuple[bool, str]:
        rc, out, err = _run(["nginx", "-t"])
        msg = (out + err).strip()
        return rc == 0, msg

    def reload(self) -> Tuple[bool, str]:
        ok, msg = self.test_config()
        if not ok:
            return False, f"nginx config test failed: {msg}"
        rc, out, err = _run(["nginx", "-s", "reload"])
        if rc == 0:
            return True, "nginx reloaded"
        return False, f"nginx reload failed: {(out+err).strip()}"

    def safe_write_and_reload(self, domain, backend_url, ssl_cert, ssl_key,
                               worker_port=None, target_lang=None,
                               languages=None, routing_mode="subdirectory"):
        langs = languages or ([target_lang] if target_lang else [getattr(settings, "TARGET_LANG", "DE")])
        try:
            if routing_mode == "subdomain":
                config_path = self.write_subdomain_configs(
                    domain, backend_url, ssl_cert, ssl_key, langs, worker_port)[0]
            else:
                config_path = self.write_subdirectory_config(
                    domain, backend_url, ssl_cert, ssl_key, langs, worker_port)
            ok, msg = self.reload()
            if not ok:
                self.remove_domain_config(domain)
                self.reload()
                return False, "", msg
            return True, config_path, msg
        except Exception as exc:
            return False, "", str(exc)

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
            return [p.stem for p in enabled.iterdir()
                    if p.name.endswith(".conf") and not p.name.endswith(".http.conf")]
        except PermissionError:
            return []

    def is_nginx_running(self) -> bool:
        rc, _, _ = _run(["pgrep", "-x", "nginx"])
        return rc == 0
