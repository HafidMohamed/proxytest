"""
CDN / Object Storage Service
=============================

Abstracts where translated HTML pages are stored:

  Mode 1 — DATABASE (default, dev/small deployments):
      HTML is stored inline in the translated_pages.html TEXT column.
      Simple, no extra infra. Breaks at scale (large blobs in Postgres).

  Mode 2 — S3 / CLOUDFLARE R2 (production):
      HTML is uploaded to S3-compatible object storage.
      translated_pages.html_url stores the public CDN URL.
      The database row stays tiny — only metadata.

Configuration (add to .env):
    CDN_BACKEND=s3          # "db" (default) or "s3"
    CDN_BUCKET=my-bucket
    CDN_REGION=eu-central-1
    CDN_ACCESS_KEY=...
    CDN_SECRET_KEY=...
    CDN_ENDPOINT_URL=       # leave empty for AWS S3; set for R2/MinIO
    CDN_PUBLIC_BASE_URL=https://cdn.example.com   # public URL prefix

The S3 key pattern is:  translations/{domain}/{language}/{url_hash}.html
"""

from __future__ import annotations

import hashlib
import logging
import os
from urllib.parse import urlparse

logger = logging.getLogger("cdn_storage")

# ── Config (read from environment at import time) ──────────────────────────

CDN_BACKEND: str      = os.environ.get("CDN_BACKEND", "db").lower()
CDN_BUCKET: str       = os.environ.get("CDN_BUCKET", "")
CDN_REGION: str       = os.environ.get("CDN_REGION", "eu-central-1")
CDN_ACCESS_KEY: str   = os.environ.get("CDN_ACCESS_KEY", "")
CDN_SECRET_KEY: str   = os.environ.get("CDN_SECRET_KEY", "")
CDN_ENDPOINT_URL: str = os.environ.get("CDN_ENDPOINT_URL", "")   # for R2/MinIO
CDN_PUBLIC_BASE: str  = os.environ.get("CDN_PUBLIC_BASE_URL", "")

_s3_client = None


def _get_s3():
    global _s3_client
    if _s3_client is None:
        try:
            import boto3
            kwargs = dict(
                region_name          = CDN_REGION,
                aws_access_key_id    = CDN_ACCESS_KEY,
                aws_secret_access_key= CDN_SECRET_KEY,
            )
            if CDN_ENDPOINT_URL:
                kwargs["endpoint_url"] = CDN_ENDPOINT_URL
            _s3_client = boto3.client("s3", **kwargs)
        except ImportError:
            logger.error("boto3 not installed — cannot use S3/CDN backend. "
                         "Run: pip install boto3")
            raise
    return _s3_client


def _s3_key(domain: str, language: str, url: str) -> str:
    url_hash = hashlib.sha256(url.encode()).hexdigest()[:16]
    lang = language.lower()
    return f"translations/{domain}/{lang}/{url_hash}.html"


def _public_url(key: str) -> str:
    if CDN_PUBLIC_BASE:
        return f"{CDN_PUBLIC_BASE.rstrip('/')}/{key}"
    # Fall back to direct S3 URL
    if CDN_ENDPOINT_URL:
        return f"{CDN_ENDPOINT_URL.rstrip('/')}/{CDN_BUCKET}/{key}"
    return f"https://{CDN_BUCKET}.s3.{CDN_REGION}.amazonaws.com/{key}"


# ── Public API ─────────────────────────────────────────────────────────────

def is_cdn_enabled() -> bool:
    return CDN_BACKEND == "s3" and bool(CDN_BUCKET)


def store_html(domain: str, language: str, url: str, html: str) -> tuple[str | None, str | None]:
    """
    Store translated HTML.

    Returns:
        (inline_html, cdn_url) — exactly one will be non-None.
        Use inline_html when CDN is disabled (store in DB column).
        Use cdn_url when CDN is enabled (store URL in DB, HTML in S3).
    """
    if not is_cdn_enabled():
        return html, None

    key = _s3_key(domain, language, url)
    try:
        s3 = _get_s3()
        s3.put_object(
            Bucket      = CDN_BUCKET,
            Key         = key,
            Body        = html.encode("utf-8"),
            ContentType = "text/html; charset=utf-8",
            # Public read — served by CDN; adjust if you use signed URLs
            ACL         = "public-read",
            CacheControl= "public, max-age=3600",
            Metadata    = {
                "domain":   domain,
                "language": language,
                "source":   url[:512],
            },
        )
        pub_url = _public_url(key)
        logger.debug("Stored %s [%s] → %s", url, language, pub_url)
        return None, pub_url
    except Exception as exc:
        logger.error("S3 upload failed for %s [%s]: %s — falling back to DB", url, language, exc)
        return html, None


def fetch_html(domain: str, language: str, url: str) -> str | None:
    """
    Fetch translated HTML from CDN/S3.
    Returns HTML string, or None if not found / CDN disabled.
    """
    if not is_cdn_enabled():
        return None

    key = _s3_key(domain, language, url)
    try:
        s3 = _get_s3()
        obj = s3.get_object(Bucket=CDN_BUCKET, Key=key)
        return obj["Body"].read().decode("utf-8")
    except Exception as exc:
        logger.debug("CDN fetch miss for %s [%s]: %s", url, language, exc)
        return None


def delete_html(domain: str, language: str, url: str) -> None:
    """Delete a translated page from CDN (call on config delete / manual purge)."""
    if not is_cdn_enabled():
        return
    key = _s3_key(domain, language, url)
    try:
        _get_s3().delete_object(Bucket=CDN_BUCKET, Key=key)
    except Exception as exc:
        logger.warning("CDN delete failed for %s: %s", key, exc)


def delete_domain(domain: str) -> int:
    """Delete ALL objects for a domain. Returns count deleted."""
    if not is_cdn_enabled():
        return 0
    prefix = f"translations/{domain}/"
    s3 = _get_s3()
    deleted = 0
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=CDN_BUCKET, Prefix=prefix):
        objects = page.get("Contents", [])
        if not objects:
            continue
        s3.delete_objects(
            Bucket=CDN_BUCKET,
            Delete={"Objects": [{"Key": o["Key"]} for o in objects]},
        )
        deleted += len(objects)
    logger.info("Deleted %d CDN objects for domain %s", deleted, domain)
    return deleted
