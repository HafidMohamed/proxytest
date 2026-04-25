"""
Glossary Service
================

Applies customer-defined glossary rules to text before/after translation:

  1. PROTECT phase  — replace glossary terms with placeholders so DeepL
                      never sees or modifies them.
  2. RESTORE phase  — put the correct (possibly language-specific) value
                      back after translation.

Rules with replacement=None are treated as "do not translate" — the original
term is kept verbatim in every language.

Rules with replacement set are language-specific substitutions — useful for
brand names that have different spellings in different markets.

Example:
  source_term="Acme Corp", replacement=None          → "Acme Corp" everywhere
  source_term="colour",    replacement="color" (EN-US) → swap British → American
"""

from __future__ import annotations

import re
import logging
import uuid
from typing import Optional
from sqlalchemy.orm import Session

logger = logging.getLogger("glossary")

# Placeholder format — unique enough to survive DeepL translation intact
_PLACEHOLDER_PREFIX = "⟦G"
_PLACEHOLDER_SUFFIX = "⟧"


def _make_placeholder(idx: int) -> str:
    return f"{_PLACEHOLDER_PREFIX}{idx:04d}{_PLACEHOLDER_SUFFIX}"


def load_rules(db: Session, domain_id: str, language: str | None = None):
    """Load all glossary rules for a domain, optionally filtered by language."""
    from ..models import GlossaryRule
    from sqlalchemy import or_

    q = db.query(GlossaryRule).filter(GlossaryRule.domain_id == domain_id)
    if language:
        # Include rules for this specific language AND rules that apply to all (NULL)
        q = q.filter(
            or_(GlossaryRule.language == language.upper(),
                GlossaryRule.language == None)  # noqa: E711
        )
    return q.all()


def protect_texts(
    texts: list[str],
    rules: list,
) -> tuple[list[str], dict[str, str]]:
    """
    Replace glossary terms in texts with unique placeholders.

    Returns:
        protected_texts: texts with terms swapped for placeholders
        restore_map: {placeholder → replacement_or_original}
    """
    if not rules:
        return texts, {}

    restore_map: dict[str, str] = {}
    counter = [0]  # mutable for closure

    def _replace_term(text: str, rule) -> str:
        term = rule.source_term
        flags = 0 if rule.case_sensitive else re.IGNORECASE
        replacement_value = rule.replacement if rule.replacement else rule.source_term

        def _sub(m: re.Match) -> str:
            ph = _make_placeholder(counter[0])
            counter[0] += 1
            restore_map[ph] = replacement_value
            return ph

        try:
            return re.sub(re.escape(term), _sub, text, flags=flags)
        except re.error as exc:
            logger.warning("Glossary regex error for term '%s': %s", term, exc)
            return text

    protected = []
    for text in texts:
        t = text
        for rule in rules:
            t = _replace_term(t, rule)
        protected.append(t)

    return protected, restore_map


def restore_texts(texts: list[str], restore_map: dict[str, str]) -> list[str]:
    """
    Swap placeholders back to their final values in translated texts.
    """
    if not restore_map:
        return texts

    result = []
    for text in texts:
        t = text
        for placeholder, value in restore_map.items():
            t = t.replace(placeholder, value)
        result.append(t)
    return result


def apply_glossary(
    db: Session,
    texts: list[str],
    domain_id: str,
    language: str,
) -> tuple[list[str], dict[str, str]]:
    """
    Convenience: load rules and protect texts in one call.
    Returns (protected_texts, restore_map) — pass restore_map to restore_texts()
    after DeepL translation.
    """
    rules = load_rules(db, domain_id, language)
    if not rules:
        return texts, {}
    return protect_texts(texts, rules)
