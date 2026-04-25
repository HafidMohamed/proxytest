"""
Auth & API Key Service
=======================

API key lifecycle:
  1. generate()  — create raw key + compute its hash
  2. verify()    — hash an incoming key, look up the hash in DB
  3. hash_key()  — one-way SHA-256 (no bcrypt; keys are random, salting not needed)

The raw key is returned ONCE at customer creation and never stored.
The DB stores only the SHA-256 hash and an 8-char prefix for display.

Format:  sk-{32-char-urlsafe-random}
Example: sk-X7fJ2mKpQr8cWnLvByZeAo1dTiHsGuNx

Rate limiting:
  slowapi is wired to the FastAPI app in main.py.
  Limits defined here as constants for central management.
"""

from __future__ import annotations

import hashlib
import secrets
import logging

logger = logging.getLogger("auth")

# ── API Key ────────────────────────────────────────────────────────────────

PREFIX = "sk-"
KEY_BYTES = 32   # 256 bits of entropy


def generate() -> tuple[str, str, str]:
    """
    Generate a new API key.

    Returns:
        (raw_key, key_hash, key_prefix)
        Store key_hash and key_prefix in DB.
        Return raw_key to the customer — it is never stored.
    """
    raw = PREFIX + secrets.token_urlsafe(KEY_BYTES)
    h   = hash_key(raw)
    prefix = raw[:len(PREFIX) + 8]   # "sk-X7fJ2mKp"
    return raw, h, prefix


def hash_key(raw_key: str) -> str:
    """SHA-256 of the raw key (hex string, 64 chars)."""
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def verify(raw_key: str, stored_hash: str) -> bool:
    """Constant-time comparison of computed hash vs stored hash."""
    return secrets.compare_digest(hash_key(raw_key), stored_hash)


# ── Rate limit constants (used in main.py with slowapi) ──────────────────

RATE_CUSTOMER_CREATE   = "3/minute"    # POST /customers — prevent account farming
RATE_DOMAIN_REGISTER   = "10/minute"   # POST /domains
RATE_VERIFY            = "20/minute"   # POST /domains/{d}/verify
RATE_PROVISION_SSL     = "5/minute"    # POST /domains/{d}/provision-ssl
RATE_CRAWL_NOW         = "5/minute"    # POST /translation/{d}/crawl-now
RATE_GLOBAL_PER_IP     = "200/minute"  # default for all other endpoints
