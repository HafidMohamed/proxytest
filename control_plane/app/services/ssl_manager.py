"""
SSL certificate lifecycle manager.

Uses certbot (Let's Encrypt) webroot plugin.
certbot runs via sudo so it can write to /etc/letsencrypt.

Permission model
----------------
certbot writes certs as root:root with 700 permissions by default.
After every issuance/renewal we chmod 755 the per-domain dirs so
www-data (the app user) can read them.
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

def _run(cmd: list, timeout: int = 120) -> Tuple[int, str, str]:
    """
    Run a command via sudo with full exception handling.

    BUG FIX: Original had no try/except — if certbot was not installed,
    subprocess.run raised FileNotFoundError which propagated all the way up
    through provision_ssl with no JSON error response, leaving clients with
    a blank HTTP response body ("Expecting value: line 1 column 1 (char 0)").
    Now we catch all subprocess exceptions and return a (1, "", error_msg)
    tuple so provision_ssl can generate a proper HTTPException(500).
    """
    full_cmd = ["sudo"] + [str(c) for c in cmd]
    logger.info("Running: %s", " ".join(full_cmd))
    try:
        result = subprocess.run(full_cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            logger.warning(
                "Command failed (%d):\nSTDOUT: %s\nSTDERR: %s",
                result.returncode, result.stdout, result.stderr,
            )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        logger.error("Command timed out after %ds: %s", timeout, " ".join(full_cmd))
        return 1, "", f"Command timed out after {timeout}s: {full_cmd[1]}"
    except FileNotFoundError as exc:
        logger.error("Command not found: %s — is it installed?", full_cmd[1])
        return 1, "", (
            f"'{full_cmd[1]}' not found. "
            "Run: apt-get install -y certbot"
        )
    except Exception as exc:
        logger.exception("Unexpected error running %s", full_cmd)
        return 1, "", f"Unexpected error: {exc}"


def _certbot_dirs() -> list:
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
    """
    live_dir    = f"{settings.CERTBOT_CONFIG_DIR}/live/{domain}"
    archive_dir = f"{settings.CERTBOT_CONFIG_DIR}/archive/{domain}"

    _run(["chmod", "755", f"{settings.CERTBOT_CONFIG_DIR}/live"],    timeout=5)
    _run(["chmod", "755", f"{settings.CERTBOT_CONFIG_DIR}/archive"], timeout=5)

    for d in [live_dir, archive_dir]:
        if Path(d).exists():
            _run(["chmod", "-R", "755", d], timeout=5)

    logger.info("Fixed cert permissions for %s", domain)


def _fix_all_cert_permissions() -> None:
    """Fix permissions for ALL customer domains after a bulk renewal."""
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
        # BUG FIX: use full path to nginx binary so the deploy hook works
        # regardless of what PATH certbot inherits when run via sudo.
        "--deploy-hook", "/usr/sbin/nginx -s reload",
    ] + _certbot_dirs()

    rc, stdout, stderr = _run(cmd, timeout=180)

    if rc == 0:
        _fix_cert_permissions(domain)
        fullchain, _ = cert_paths(domain)
        logger.info("Certificate issued for %s: %s", domain, fullchain)
        return True, f"Certificate issued: {fullchain}"

    # Build a clear error summary from certbot output
    combined = (stderr + "\n" + stdout).strip()
    relevant = [
        line.strip() for line in combined.splitlines()
        if any(k in line.lower() for k in
               ["error", "challenge", "failed", "connection", "timeout",
                "unauthorized", "problem", "could not", "port 80",
                "not found", "installed"])
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

    try:
        fpath.parent.mkdir(parents=True, exist_ok=True)
        fpath.write_text("ok")
    except Exception as exc:
        return False, (
            f"Cannot write to ACME webroot {webroot}: {exc}. "
            "Run: chown -R www-data:www-data /var/www/acme-challenge"
        )

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
            "Check nginx serves /.well-known/acme-challenge/ on port 80."
        )
    except Exception as e:
        issues.append(
            f"Cannot reach nginx on localhost:80 — is nginx running? "
            f"Run: systemctl status nginx  Error: {e}"
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
    try:
        result = subprocess.run(
            ["openssl", "x509", "-enddate", "-noout", "-in", fullchain],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return None
        date_str = result.stdout.strip().split("=", 1)[1]
        return datetime.strptime(date_str, "%b %d %H:%M:%S %Y %Z").replace(
            tzinfo=timezone.utc)
    except Exception as exc:
        logger.warning("Could not read cert expiry for %s: %s", domain, exc)
        return None


def renew_all_certificates() -> dict:
    """
    Renew all customer certs expiring within 30 days.
    Fixes permissions for ALL customer domains after renewal.
    Called by the systemd certbot-proxy-renew.timer twice daily.
    """
    cmd = [
        "certbot", "renew",
        "--non-interactive",
        "--deploy-hook", "/usr/sbin/nginx -s reload",
        "-v",
    ] + _certbot_dirs()

    rc, stdout, stderr = _run(cmd, timeout=300)
    _fix_all_cert_permissions()
    return {"returncode": rc, "stdout": stdout, "stderr": stderr, "success": rc == 0}
