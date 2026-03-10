"""
SSL certificate lifecycle manager.

Uses certbot (Let's Encrypt) webroot plugin.
certbot runs via sudo so www-data can write to /etc/letsencrypt.

ACME HTTP-01 challenge flow:
  1. Certbot writes a token file to NGINX_ACME_WEBROOT
  2. Let's Encrypt servers fetch http://<domain>/.well-known/acme-challenge/<token>
  3. Nginx serves the file from the webroot (port 80, open to all)
  4. LE verifies → issues cert
  ⚠ Port 80 must be open to ALL IPs, not just Cloudflare.
"""

import logging
import subprocess
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Tuple

from ..config import settings

logger = logging.getLogger(__name__)


def _run(cmd: list[str], timeout: int = 120) -> Tuple[int, str, str]:
    full_cmd = ["sudo"] + cmd
    logger.info("Running: %s", " ".join(full_cmd))
    result = subprocess.run(full_cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        logger.warning("Command failed (%d):\nSTDOUT: %s\nSTDERR: %s",
                       result.returncode, result.stdout, result.stderr)
    return result.returncode, result.stdout, result.stderr


def _certbot_dirs() -> list[str]:
    return [
        "--config-dir", settings.CERTBOT_CONFIG_DIR,
        "--work-dir",   settings.CERTBOT_WORK_DIR,
        "--logs-dir",   settings.CERTBOT_LOGS_DIR,
    ]


def cert_dir(domain: str) -> Path:
    return Path(settings.CERTBOT_CONFIG_DIR) / "live" / domain


def cert_exists(domain: str) -> bool:
    d = cert_dir(domain)
    return (d / "fullchain.pem").exists() and (d / "privkey.pem").exists()


def cert_paths(domain: str) -> Tuple[str, str]:
    d = cert_dir(domain)
    return str(d / "fullchain.pem"), str(d / "privkey.pem")


def _get_nginx_reload_hook() -> str:
    """Return a deploy-hook shell snippet that reloads nginx."""
    return "nginx -s reload"


def issue_certificate(domain: str) -> Tuple[bool, str]:
    """
    Issue a Let's Encrypt certificate via HTTP-01 webroot challenge.

    Requirements:
      - Port 80 is open to ALL IPs (LE validators are NOT Cloudflare IPs)
      - Nginx is running and serving the acme webroot on port 80
      - The domain A record points to this server

    Returns (success, message).
    """
    webroot = settings.NGINX_ACME_WEBROOT
    os.makedirs(webroot, exist_ok=True)

    # Write a test file to confirm nginx is serving the webroot
    test_file = Path(webroot) / ".well-known" / "acme-challenge" / "nginx-test"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("ok")

    cmd = [
        "certbot", "certonly",
        "--webroot",
        "--webroot-path", webroot,
        "--domain", domain,
        "--email", settings.CERTBOT_EMAIL,
        "--agree-tos",
        "--non-interactive",
        "--keep-until-expiring",
        "-v",                                        # verbose – shows exact failure reason
        "--deploy-hook", _get_nginx_reload_hook(),   # reload nginx after issue/renewal
    ] + _certbot_dirs()

    rc, stdout, stderr = _run(cmd, timeout=180)

    # Clean up test file
    try:
        test_file.unlink()
    except Exception:
        pass

    if rc == 0:
        fullchain, _ = cert_paths(domain)
        logger.info("Certificate issued for %s: %s", domain, fullchain)
        return True, f"Certificate issued: {fullchain}"

    # Build a clear error message from certbot output
    combined = (stderr + "\n" + stdout).strip()
    # Extract the most relevant line
    relevant = []
    for line in combined.splitlines():
        if any(k in line.lower() for k in
               ["error", "challenge", "failed", "connection", "timeout",
                "unauthorized", "problem", "could not", "port 80"]):
            relevant.append(line.strip())
    summary = "\n".join(relevant[:8]) if relevant else combined[:600]

    logger.error("certbot failed for %s:\n%s", domain, combined[:2000])
    return False, f"certbot error: {summary}"


def pre_issue_checks(domain: str) -> Tuple[bool, str]:
    """
    Validate that the ACME challenge can succeed BEFORE calling certbot.
    Returns (ok, error_message).
    """
    import urllib.request
    import urllib.error

    webroot = settings.NGINX_ACME_WEBROOT
    token   = "pre-check-test-token"
    path    = Path(webroot) / ".well-known" / "acme-challenge" / token

    issues = []

    # 1. Write test token
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("ok")

    # 2. Try to fetch it over HTTP
    try:
        url = f"http://{domain}/.well-known/acme-challenge/{token}"
        req = urllib.request.Request(url, headers={"User-Agent": "proxy-pre-check/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode()
            if body.strip() != "ok":
                issues.append(f"Webroot test file returned unexpected content: {body[:100]}")
    except urllib.error.HTTPError as e:
        issues.append(f"HTTP {e.code} fetching ACME test URL {url}")
    except Exception as e:
        issues.append(
            f"Cannot reach http://{domain}/.well-known/acme-challenge/ — "
            f"port 80 may be blocked or nginx not running. Error: {e}"
        )
    finally:
        try:
            path.unlink()
        except Exception:
            pass

    if issues:
        return False, "\n".join(issues)
    return True, "Pre-checks passed: port 80 reachable and webroot serving correctly"


def revoke_and_delete_certificate(domain: str) -> Tuple[bool, str]:
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
    Renew all certs expiring within 30 days.
    --deploy-hook reloads nginx after each successful renewal.
    Called by cron twice daily (renew_certs.sh).
    """
    cmd = [
        "certbot", "renew",
        "--non-interactive",
        "--deploy-hook", _get_nginx_reload_hook(),
        "-v",
    ] + _certbot_dirs()
    rc, stdout, stderr = _run(cmd, timeout=300)
    return {"returncode": rc, "stdout": stdout, "stderr": stderr, "success": rc == 0}
