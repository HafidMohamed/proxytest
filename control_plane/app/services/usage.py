"""
Usage & Billing Metering Service
==================================
Records usage events for metered billing. All writes are append-only.
"""

from __future__ import annotations

import logging
import uuid as _uuid_mod
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy import func, extract

logger = logging.getLogger("usage")

PLAN_LIMITS: dict[str, dict] = {
    "free":       {"monthly_words": 2_000,      "monthly_pages": 0},
    "starter":    {"monthly_words": 10_000,     "monthly_pages": 0},
    "business":   {"monthly_words": 50_000,     "monthly_pages": 0},
    "pro":        {"monthly_words": 200_000,    "monthly_pages": 0},
    "enterprise": {"monthly_words": 10_000_000, "monthly_pages": 0},
}


def _to_uuid(val) -> _uuid_mod.UUID:
    """Safely convert str or UUID to uuid.UUID."""
    if isinstance(val, _uuid_mod.UUID):
        return val
    return _uuid_mod.UUID(str(val))


def record_words(
    db: Session,
    customer_id,
    domain_id,
    word_count: int,
    language: str,
    url: str | None = None,
) -> None:
    if word_count <= 0:
        return
    from ..models import UsageEvent, UsageEventType
    event = UsageEvent(
        id          = _uuid_mod.uuid4(),
        customer_id = _to_uuid(customer_id),
        domain_id   = _to_uuid(domain_id) if domain_id else None,
        event_type  = UsageEventType.WORDS_TRANSLATED,
        quantity    = word_count,
        language    = language,
        url         = (url or "")[:2048],
        occurred_at = datetime.now(timezone.utc),
    )
    db.add(event)
    try:
        db.commit()
    except Exception as exc:
        logger.warning("Usage record failed (non-fatal): %s", exc)
        db.rollback()


def record_page_served(
    db: Session,
    customer_id,
    domain_id,
    language: str,
    url: str,
) -> None:
    from ..models import UsageEvent, UsageEventType
    event = UsageEvent(
        id          = _uuid_mod.uuid4(),
        customer_id = _to_uuid(customer_id),
        domain_id   = _to_uuid(domain_id) if domain_id else None,
        event_type  = UsageEventType.PAGE_SERVED,
        quantity    = 1,
        language    = language,
        url         = url[:2048],
        occurred_at = datetime.now(timezone.utc),
    )
    db.add(event)
    try:
        db.commit()
    except Exception as exc:
        logger.warning("Page served event failed (non-fatal): %s", exc)
        db.rollback()


def get_monthly_usage(
    db: Session,
    customer_id,
    year: int | None = None,
    month: int | None = None,
) -> dict:
    from ..models import UsageEvent, UsageEventType

    now = datetime.now(timezone.utc)
    y = year  or now.year
    m = month or now.month
    cid = _to_uuid(customer_id)

    def _sum(event_type: UsageEventType) -> int:
        result = (
            db.query(func.sum(UsageEvent.quantity))
            .filter(
                UsageEvent.customer_id == cid,
                UsageEvent.event_type  == event_type,
                extract("year",  UsageEvent.occurred_at) == y,
                extract("month", UsageEvent.occurred_at) == m,
            )
            .scalar()
        )
        return int(result or 0)

    return {
        "year":             y,
        "month":            m,
        "words_translated": _sum(UsageEventType.WORDS_TRANSLATED),
        "pages_served":     _sum(UsageEventType.PAGE_SERVED),
        "crawls_run":       _sum(UsageEventType.CRAWL_RUN),
    }


def is_over_word_limit(db: Session, customer_id, plan: str) -> bool:
    limits = PLAN_LIMITS.get(plan, PLAN_LIMITS["free"])
    limit  = limits["monthly_words"]
    if limit <= 0:
        return False
    usage = get_monthly_usage(db, customer_id)
    return usage["words_translated"] >= limit


def get_usage_summary(db: Session, customer_id, plan: str) -> dict:
    usage  = get_monthly_usage(db, customer_id)
    limits = PLAN_LIMITS.get(plan, PLAN_LIMITS["free"])
    word_limit = limits["monthly_words"]
    used       = usage["words_translated"]
    return {
        **usage,
        "plan":                 plan,
        "word_limit":           word_limit,
        "word_limit_unlimited": word_limit <= 0,
        "word_percent_used":    round(used / word_limit * 100, 1) if word_limit > 0 else 0,
        "over_limit":           used >= word_limit if word_limit > 0 else False,
    }
