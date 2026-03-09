"""
DNS verification helpers.

Two checks are required before we issue an SSL cert:
  1. TXT ownership check  – customer placed our token at _proxy-verify.<domain>
  2. A/CNAME points to us – so the ACME HTTP-01 challenge will reach this server
"""

import socket
import logging
from typing import Tuple

import dns.resolver
import dns.exception

from ..config import settings

logger = logging.getLogger(__name__)


def _get_txt_records(hostname: str) -> list[str]:
    """Return all TXT values for *hostname*, or [] on NXDOMAIN / timeout."""
    resolver = dns.resolver.Resolver()
    resolver.lifetime = settings.DNS_CHECK_TIMEOUT
    try:
        answers = resolver.resolve(hostname, "TXT")
        results = []
        for rdata in answers:
            for txt_str in rdata.strings:
                results.append(txt_str.decode("utf-8", errors="replace"))
        return results
    except (dns.exception.DNSException, Exception) as exc:
        logger.debug("TXT lookup failed for %s: %s", hostname, exc)
        return []


def _get_a_record(hostname: str) -> str | None:
    """Return first A-record IP for *hostname*, or None."""
    resolver = dns.resolver.Resolver()
    resolver.lifetime = settings.DNS_CHECK_TIMEOUT
    try:
        answers = resolver.resolve(hostname, "A")
        return str(answers[0])
    except Exception as exc:
        logger.debug("A lookup failed for %s: %s", hostname, exc)
        return None


# ── Public API ────────────────────────────────────────────────────────────────

def check_dns_txt_verification(domain: str, expected_token: str) -> Tuple[bool, str]:
    """
    Check that `_proxy-verify.<domain>` contains the expected token.
    Returns (success, human-readable message).
    """
    txt_host = f"{settings.VERIFICATION_TXT_PREFIX}.{domain}"
    expected_value = f"proxy-verify={expected_token}"

    records = _get_txt_records(txt_host)
    logger.info("TXT records at %s: %s", txt_host, records)

    if expected_value in records:
        return True, f"Token found at {txt_host}"

    if records:
        return False, (
            f"Token NOT found at {txt_host}. "
            f"Found: {records}. Expected: {expected_value}"
        )
    return False, f"No TXT records found at {txt_host}"


def check_domain_points_to_us(domain: str) -> Tuple[bool, str]:
    """
    Check that the domain's A record resolves to our public IP.
    This is required for the ACME HTTP-01 challenge to succeed.
    """
    if settings.OUR_SERVER_IP in ("0.0.0.0", "127.0.0.1"):
        # In dev/test mode skip IP check
        return True, "IP check skipped (dev mode)"

    resolved_ip = _get_a_record(domain)
    if resolved_ip is None:
        return False, f"Could not resolve A record for {domain}"

    if resolved_ip == settings.OUR_SERVER_IP:
        return True, f"{domain} → {resolved_ip} ✓"

    return False, (
        f"{domain} resolves to {resolved_ip}, "
        f"expected {settings.OUR_SERVER_IP}. "
        "Please update your A record."
    )


def full_domain_check(domain: str, token: str) -> Tuple[bool, dict]:
    """
    Run both checks. Returns (all_passed, details_dict).
    """
    txt_ok, txt_msg   = check_dns_txt_verification(domain, token)
    ip_ok,  ip_msg    = check_domain_points_to_us(domain)

    return (txt_ok and ip_ok), {
        "txt_check": {"passed": txt_ok, "message": txt_msg},
        "ip_check":  {"passed": ip_ok,  "message": ip_msg},
    }
