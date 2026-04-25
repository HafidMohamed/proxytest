"""
Translation Memory Service
==========================

Provides a two-level translation cache:
  Level 1 (in-process): asyncio-safe LRU dict — sub-millisecond lookup.
  Level 2 (PostgreSQL):  TranslationMemory table — survives restarts and
                         is shared across all worker processes.

When a text is found in either level it is returned immediately — DeepL is
never called, saving API quota and latency.

Integration in translate_html():
  1. Split texts into CACHE HIT (from TM) and CACHE MISS lists.
  2. Send only misses to DeepL.
  3. Store misses back into TM for future use.
  4. Recombine in original order.
"""

from __future__ import annotations

import hashlib
import logging
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

logger = logging.getLogger("translation_memory")

# ── In-process LRU (hot cache) ─────────────────────────────────────────────

_HOT_CACHE_MAX = 50_000   # entries
_hot: OrderedDict[str, str] = OrderedDict()


def _hot_key(source_hash: str, language: str) -> str:
    return f"{language}:{source_hash}"


def _hot_get(source_hash: str, language: str) -> str | None:
    k = _hot_key(source_hash, language)
    if k in _hot:
        _hot.move_to_end(k)
        return _hot[k]
    return None


def _hot_set(source_hash: str, language: str, translation: str) -> None:
    k = _hot_key(source_hash, language)
    _hot[k] = translation
    _hot.move_to_end(k)
    while len(_hot) > _HOT_CACHE_MAX:
        _hot.popitem(last=False)


# ── Public API ─────────────────────────────────────────────────────────────

def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def lookup(db: Session, source_text: str, language: str) -> str | None:
    """
    Return cached translation if available, else None.
    Checks hot cache first, then DB.
    """
    from ..models import TranslationMemory
    h = text_hash(source_text)

    # Level 1 — hot cache
    hit = _hot_get(h, language)
    if hit is not None:
        return hit

    # Level 2 — database
    row = (
        db.query(TranslationMemory)
        .filter_by(source_hash=h, language=language)
        .first()
    )
    if row:
        # Increment hit counter (best-effort, don't fail translation on error)
        try:
            row.hit_count += 1
            db.commit()
        except Exception:
            db.rollback()
        _hot_set(h, language, row.translated_text)
        return row.translated_text

    return None


def store(db: Session, source_text: str, language: str, translated_text: str) -> None:
    """
    Persist a new translation to the memory table.
    Uses INSERT ... ON CONFLICT DO UPDATE so it's safe to call concurrently.
    """
    from ..models import TranslationMemory
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    h = text_hash(source_text)

    stmt = pg_insert(TranslationMemory).values(
        source_hash     = h,
        source_text     = source_text,
        language        = language,
        translated_text = translated_text,
        hit_count       = 0,
        created_at      = datetime.now(timezone.utc),
        updated_at      = datetime.now(timezone.utc),
    ).on_conflict_do_update(
        constraint="uq_tm_hash_lang",
        set_={"translated_text": translated_text,
              "updated_at": datetime.now(timezone.utc)},
    )
    try:
        db.execute(stmt)
        db.commit()
    except Exception as exc:
        logger.warning("TM store failed (non-fatal): %s", exc)
        db.rollback()

    _hot_set(h, language, translated_text)


def bulk_lookup(
    db: Session, texts: list[str], language: str
) -> dict[str, str | None]:
    """
    Look up multiple texts at once.
    Returns {source_text: translation_or_None}.
    Uses a single IN query for the DB level.
    """
    from ..models import TranslationMemory

    result: dict[str, str | None] = {t: None for t in texts}
    hashes = {text_hash(t): t for t in texts}

    # Hot cache pass
    uncached_hashes: list[str] = []
    for h, txt in hashes.items():
        hit = _hot_get(h, language)
        if hit is not None:
            result[txt] = hit
        else:
            uncached_hashes.append(h)

    if not uncached_hashes:
        return result

    # DB pass — single query
    rows = (
        db.query(TranslationMemory)
        .filter(
            TranslationMemory.language == language,
            TranslationMemory.source_hash.in_(uncached_hashes),
        )
        .all()
    )
    for row in rows:
        original = hashes.get(row.source_hash)
        if original:
            result[original] = row.translated_text
            _hot_set(row.source_hash, language, row.translated_text)

    return result


def stats(db: Session) -> dict:
    """Return basic TM statistics."""
    from ..models import TranslationMemory
    from sqlalchemy import func
    total = db.query(func.count(TranslationMemory.id)).scalar()
    total_hits = db.query(func.sum(TranslationMemory.hit_count)).scalar() or 0
    by_lang = (
        db.query(TranslationMemory.language, func.count(TranslationMemory.id))
        .group_by(TranslationMemory.language)
        .all()
    )
    return {
        "total_entries": total,
        "total_cache_hits": int(total_hits),
        "hot_cache_size": len(_hot),
        "by_language": {lang: count for lang, count in by_lang},
    }
