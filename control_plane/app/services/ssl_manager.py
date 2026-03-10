"""
SSL certificate lifecycle manager.

Uses certbot (Let's Encrypt) webroot plugin.
certbot runs via sudo so www-data can write to /etc/letsencrypt.

Permission model
----------------
certbot writes certs as root:root with 700 permissions by default.
After every issuance/renewal we chmod 755 the per-domain dirs so
www-data (the app user) can read them.
This is done for EVERY customer domain dynamically — never hardcoded.
"""

import logging
import subprocess
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Tuple

from ..config import settings

logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _run(cmd: list[str], timeout: int = 120) -> Tuple[int, str, str]:
    """Run command via sudo, return (returncode, stdout, stderr)."""
    full_cmd = ["sudo"] + cmd
    logger.info("Running: %s", " ".join(full_cmd))
    result = subprocess.run(full_cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        logger.warning("Command failed (%d):\nSTDOUT: %s\nSTDERR: %s",
                       result.returncode, result.stdout, result.stderr)
    return result.returncode, result.stdout, result.stderr


def _certbot_dirs() -> list[str]:
    """Common certbot dir flags — prevents read-only filesystem errors."""
    return [
        "--config-dir", settings.CERTBOT_CONFIG_DIR,
        "--work-dir",   settings.CERTBOT_WORK_DIR,
        "--logs-dir",   settings.CERTBOT_LOGS_DIR,
    ]


def _fix_cert_permissions(domain: str) -> None:
    """
    Fix letsencrypt directory permissions for a specific customer domain.

    certbot writes certs as root:root 700.  www-data needs read access.
    Called after every issuance and renewal for each domain.

    This is intentionally per-domain — not a blanket chmod on all of
    /etc/letsencrypt — so we only expose the minimum needed.
    """
    live_dir    = f"{settings.CERTBOT_CONFIG_DIR}/live/{domain}"
    archive_dir = f"{settings.CERTBOT_CONFIG_DIR}/archive/{domain}"

    # Parent dirs need execute bit so www-data can traverse them
    _run(["chmod", "755", f"{settings.CERTBOT_CONFIG_DIR}/live"],    timeout=5)
    _run(["chmod", "755", f"{settings.CERTBOT_CONFIG_DIR}/archive"], timeout=5)

    # Per-customer cert dirs
    for d in [live_dir, archive_dir]:
        if Path(d).exists():
            _run(["chmod", "-R", "755", d], timeout=5)

    logger.info("Fixed cert permissions for %s", domain)


def _fix_all_cert_permissions() -> None:
    """
    Fix permissions for ALL customer domains after a bulk renewal.
    Scans /etc/letsencrypt/live/ and fixes each domain found.
    """
    live_root = Path(settings.CERTBOT_CONFIG_DIR) / "live"
    if not live_root.exists():
        return

    _run(["chmod", "755", str(live_root)], timeout=5)
    _run(["chmod", "755", str(live_root.parent / "archive")], timeout=5)

    try:
        domains = [
            d.name for d in live_root.iterdir()
            if d.is_dir() and d.name != "README"
        ]
    except PermissionError:
        # Can't list — fix the parent first then retry
        _run(["chmod", "755", str(live_root)], timeout=5)
        domains = []

    for domain in domains:
        _fix_cert_permissions(domain)
        logger.info("Fixed permissions for customer domain: %s", domain)


# ── Public cert helpers ───────────────────────────────────────────────────────

def cert_dir(domain: str) -> Path:
    return Path(settings.CERTBOT_CONFIG_DIR) / "live" / domain


def cert_exists(domain: str) -> bool:
    """Check cert exists. Returns False on PermissionError instead of crashing."""
    d = cert_dir(domain)
    try:
        return (d / "fullchain.pem").exists() and (d / "privkey.pem").exists()
    except PermissionError:
        # Cert exists but www-data can't read it — fix and retry
        logger.warning("PermissionError reading cert for %s — fixing permissions", domain)
        _fix_cert_permissions(domain)
        try:
            return (d / "fullchain.pem").exists() and (d / "privkey.pem").exists()
        except PermissionError:
            return False


def cert_paths(domain: str) -> Tuple[str, str]:
    """Return (fullchain_path, privkey_path) for a customer domain."""
    d = cert_dir(domain)
    return str(d / "fullchain.pem"), str(d / "privkey.pem")


# ── Certificate operations ────────────────────────────────────────────────────

def issue_certificate(domain: str) -> Tuple[bool, str]:
    """
    Issue a Let's Encrypt certificate via HTTP-01 webroot challenge.

    Requirements:
      - Port 80 open to ALL IPs (LE validators are not Cloudflare IPs)
      - Nginx serving /.well-known/acme-challenge/ on port 80
      - Domain A record points to this server

    Returns (success, message).
    """
    webroot = settings.NGINX_ACME_WEBROOT
    os.makedirs(webroot, exist_ok=True)

    cmd = [
        "certbot", "certonly",
        "--webroot",
        "--webroot-path", webroot,
        "--domain", domain,
        "--email", settings.CERTBOT_EMAIL,
        "--agree-tos",
        "--non-interactive",
        "--keep-until-expiring",
        "-v",
        "--deploy-hook", "nginx -s reload",
    ] + _certbot_dirs()

    rc, stdout, stderr = _run(cmd, timeout=180)

    if rc == 0:
        # Fix permissions for THIS customer's domain immediately after issuance
        _fix_cert_permissions(domain)
        fullchain, _ = cert_paths(domain)
        logger.info("Certificate issued for %s: %s", domain, fullchain)
        return True, f"Certificate issued: {fullchain}"

    # Build clear error summary from certbot output
    combined = (stderr + "\n" + stdout).strip()
    relevant = [
        line.strip() for line in combined.splitlines()
        if any(k in line.lower() for k in
               ["error", "challenge", "failed", "connection", "timeout",
                "unauthorized", "problem", "could not", "port 80"])
    ]
    summary = "\n".join(relevant[:8]) if relevant else combined[:600]
    logger.error("certbot failed for %s:\n%s", domain, combined[:2000])
    return False, f"certbot error: {summary}"


def pre_issue_checks(domain: str) -> Tuple[bool, str]:
    """
    Validate ACME challenge will succeed BEFORE calling certbot.
    Tests via localhost so firewall/DNS don't interfere.
    """
    import urllib.request
    import urllib.error

    webroot = settings.NGINX_ACME_WEBROOT
    token   = "pre-check-proxy-test"
    fpath   = Path(webroot) / ".well-known" / "acme-challenge" / token

    fpath.parent.mkdir(parents=True, exist_ok=True)
    fpath.write_text("ok")

    issues = []
    try:
        url = f"http://127.0.0.1/.well-known/acme-challenge/{token}"
        req = urllib.request.Request(
            url, headers={"Host": domain, "User-Agent": "proxy-pre-check/1.0"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode().strip()
            if body != "ok":
                issues.append(
                    f"Webroot served wrong content: {body[:50]!r}. "
                    "Check /var/www/acme-challenge permissions."
                )
    except urllib.error.HTTPError as e:
        issues.append(
            f"Nginx returned HTTP {e.code} for ACME challenge. "
            "Check nginx default server block serves /.well-known/acme-challenge/"
        )
    except Exception as e:
        issues.append(
            f"Cannot reach nginx on localhost:80 — is nginx running? "
            f"Run: systemctl status nginx. Error: {e}"
        )
    finally:
        try:
            fpath.unlink()
        except Exception:
            pass

    if issues:
        return False, "\n".join(issues)
    return True, "Pre-checks passed: nginx serving ACME webroot correctly"


def revoke_and_delete_certificate(domain: str) -> Tuple[bool, str]:
    """Revoke and delete certificate for a specific customer domain."""
    if not cert_exists(domain):
        return True, "No certificate found to revoke"
    fullchain, _ = cert_paths(domain)
    cmd = [
        "certbot", "revoke",
        "--cert-path", fullchain,
        "--delete-after-revoke",
        "--non-interactive",
    ] + _certbot_dirs()
    rc, stdout, stderr = _run(cmd, timeout=60)
    if rc == 0:
        return True, "Certificate revoked and deleted"
    return False, (stderr or stdout).strip()


def get_cert_expiry(domain: str) -> datetime | None:
    """Read certificate expiry date for a customer domain."""
    if not cert_exists(domain):
        return None
    fullchain, _ = cert_paths(domain)
    result = subprocess.run(
        ["openssl", "x509", "-enddate", "-noout", "-in", fullchain],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        return None
    try:
        date_str = result.stdout.strip().split("=", 1)[1]
        return datetime.strptime(date_str, "%b %d %H:%M:%S %Y %Z").replace(
            tzinfo=timezone.utc)
    except Exception:
        return None


def renew_all_certificates() -> dict:
    """
    Renew all customer certs expiring within 30 days.
    Fixes permissions for ALL customer domains after renewal so
    www-data can read every renewed cert.
    Called by the systemd certbot-proxy-renew.timer twice daily.
    """
    cmd = [
        "certbot", "renew",
        "--non-interactive",
        "--deploy-hook", "nginx -s reload",
        "-v",
    ] + _certbot_dirs()

    rc, stdout, stderr = _run(cmd, timeout=300)

    # Fix permissions for ALL customer domains after bulk renewal
    _fix_all_cert_permissions()

    return {"returncode": rc, "stdout": stdout, "stderr": stderr, "success": rc == 0}
