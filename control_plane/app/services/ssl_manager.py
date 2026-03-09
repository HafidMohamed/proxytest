"""
SSL certificate lifecycle manager.

Uses certbot (Let's Encrypt) with the webroot plugin so nginx keeps running
during certificate issuance and renewal.

Webroot directory served by nginx:  /var/www/acme-challenge
Challenge URL served at:            http://<domain>/.well-known/acme-challenge/
"""

import logging
import subprocess
import shutil
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Tuple

from ..config import settings

logger = logging.getLogger(__name__)


def _run(cmd: list[str], timeout: int = 120) -> Tuple[int, str, str]:
    """Run a shell command and return (returncode, stdout, stderr)."""
    logger.info("Running: %s", " ".join(cmd))
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        logger.warning("Command failed (%d): %s", result.returncode, result.stderr)
    return result.returncode, result.stdout, result.stderr


def cert_dir(domain: str) -> Path:
    return Path(settings.LETSENCRYPT_LIVE) / domain


def cert_exists(domain: str) -> bool:
    d = cert_dir(domain)
    return (d / "fullchain.pem").exists() and (d / "privkey.pem").exists()


def cert_paths(domain: str) -> Tuple[str, str]:
    """Return (cert_path, key_path) for a domain."""
    d = cert_dir(domain)
    return str(d / "fullchain.pem"), str(d / "privkey.pem")


def issue_certificate(domain: str) -> Tuple[bool, str]:
    """
    Issue a new Let's Encrypt certificate via the webroot plugin.
    Requires:
      - nginx is running and serving /.well-known/acme-challenge/ from NGINX_ACME_WEBROOT
      - domain A-record already points here
    Returns (success, message).
    """
    # Ensure webroot directory exists
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
        "--keep-until-expiring",          # don't re-issue if still valid
        "--deploy-hook", "nginx -s reload",  # reload nginx after renewal
    ]

    rc, stdout, stderr = _run(cmd, timeout=180)
    if rc == 0:
        fullchain, privkey = cert_paths(domain)
        logger.info("Certificate issued for %s: %s", domain, fullchain)
        return True, f"Certificate issued: {fullchain}"
    else:
        err = stderr or stdout
        logger.error("certbot failed for %s: %s", domain, err)
        return False, f"certbot error: {err[:500]}"


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
    ]
    rc, stdout, stderr = _run(cmd, timeout=60)
    if rc == 0:
        return True, "Certificate revoked and deleted"
    return False, stderr or stdout


def get_cert_expiry(domain: str) -> datetime | None:
    """Read the certificate expiry date using openssl."""
    if not cert_exists(domain):
        return None
    fullchain, _ = cert_paths(domain)
    cmd = ["openssl", "x509", "-enddate", "-noout", "-in", fullchain]
    rc, stdout, _ = _run(cmd, timeout=10)
    if rc != 0:
        return None
    # stdout: "notAfter=Jun 10 12:00:00 2025 GMT"
    try:
        date_str = stdout.strip().split("=", 1)[1]
        return datetime.strptime(date_str, "%b %d %H:%M:%S %Y %Z").replace(
            tzinfo=timezone.utc
        )
    except Exception:
        return None


def renew_all_certificates() -> dict:
    """
    Run `certbot renew` to renew all certs expiring within 30 days.
    Called by the scheduled renew_certs.sh cron job, but can also be
    triggered via API.
    """
    cmd = ["certbot", "renew", "--non-interactive", "--deploy-hook", "nginx -s reload"]
    rc, stdout, stderr = _run(cmd, timeout=300)
    return {
        "returncode": rc,
        "stdout": stdout,
        "stderr": stderr,
        "success": rc == 0,
    }
