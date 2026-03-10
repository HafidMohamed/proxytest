"""
SSL certificate lifecycle manager.

Uses certbot (Let's Encrypt) with the webroot plugin so nginx keeps running
during certificate issuance and renewal.

Webroot directory served by nginx:  /var/www/acme-challenge
Challenge URL served at:            http://<domain>/.well-known/acme-challenge/

FIX: certbot is invoked via sudo so it can write to /etc/letsencrypt,
     /var/lib/letsencrypt, and /var/log/letsencrypt even when the app
     runs as www-data.  The sudoers entry added by setup.sh grants
     www-data NOPASSWD access to /usr/bin/certbot.
"""

import logging
import subprocess
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Tuple

from ..config import settings

logger = logging.getLogger(__name__)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _run(cmd: list[str], timeout: int = 120) -> Tuple[int, str, str]:
    """Run a command with sudo and return (returncode, stdout, stderr)."""
    full_cmd = ["sudo"] + cmd
    logger.info("Running: %s", " ".join(full_cmd))
    result = subprocess.run(
        full_cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        logger.warning("Command failed (%d): %s", result.returncode, result.stderr)
    return result.returncode, result.stdout, result.stderr


def _certbot_base_args() -> list[str]:
    """
    Common certbot flags pointing to LE directories.
    Prevents 'Read-only file system' error when running as www-data.
    """
    return [
        "--config-dir", settings.CERTBOT_CONFIG_DIR,
        "--work-dir",   settings.CERTBOT_WORK_DIR,
        "--logs-dir",   settings.CERTBOT_LOGS_DIR,
    ]


# ── Public helpers ────────────────────────────────────────────────────────────

def cert_dir(domain: str) -> Path:
    return Path(settings.CERTBOT_CONFIG_DIR) / "live" / domain


def cert_exists(domain: str) -> bool:
    d = cert_dir(domain)
    return (d / "fullchain.pem").exists() and (d / "privkey.pem").exists()


def cert_paths(domain: str) -> Tuple[str, str]:
    """Return (fullchain_path, privkey_path) for a domain."""
    d = cert_dir(domain)
    return str(d / "fullchain.pem"), str(d / "privkey.pem")


# ── Certificate operations ────────────────────────────────────────────────────

def issue_certificate(domain: str) -> Tuple[bool, str]:
    """
    Issue a new Let's Encrypt certificate via the webroot plugin.
    Requires:
      - nginx is running and serving /.well-known/acme-challenge/ from NGINX_ACME_WEBROOT
      - domain A-record already points to this server
    Returns (success, message).
    """
    webroot = settings.NGINX_ACME_WEBROOT
    os.makedirs(webroot, exist_ok=True)

    cmd = [
        "certbot", "certonly",
        "--webroot",
        "-w", webroot,
        "-d", domain,
        "--email", settings.CERTBOT_EMAIL,
        "--agree-tos",
        "--non-interactive",
        "--keep-until-expiring",
        "--deploy-hook", "nginx -s reload",
    ] + _certbot_base_args()

    rc, stdout, stderr = _run(cmd, timeout=180)
    if rc == 0:
        fullchain, privkey = cert_paths(domain)
        logger.info("Certificate issued for %s: %s", domain, fullchain)
        return True, f"Certificate issued: {fullchain}"

    err = (stderr or stdout).strip()
    logger.error("certbot failed for %s: %s", domain, err)
    return False, f"certbot error: {err[:600]}"


def revoke_and_delete_certificate(domain: str) -> Tuple[bool, str]:
    """Revoke and delete the certificate for a domain."""
    if not cert_exists(domain):
        return True, "No certificate found to revoke"

    fullchain, _ = cert_paths(domain)
    cmd = [
        "certbot", "revoke",
        "--cert-path", fullchain,
        "--delete-after-revoke",
        "--non-interactive",
    ] + _certbot_base_args()

    rc, stdout, stderr = _run(cmd, timeout=60)
    if rc == 0:
        return True, "Certificate revoked and deleted"
    return False, (stderr or stdout).strip()


def get_cert_expiry(domain: str) -> datetime | None:
    """Read the certificate expiry date using openssl."""
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
            tzinfo=timezone.utc
        )
    except Exception:
        return None


def renew_all_certificates() -> dict:
    """Run certbot renew for all certs expiring within 30 days."""
    cmd = [
        "certbot", "renew",
        "--non-interactive",
        "--deploy-hook", "nginx -s reload",
    ] + _certbot_base_args()

    rc, stdout, stderr = _run(cmd, timeout=300)
    return {
        "returncode": rc,
        "stdout": stdout,
        "stderr": stderr,
        "success": rc == 0,
    }
