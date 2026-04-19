"""
SEO Crawler & Translator
========================

Responsibilities:
  1. Parse sitemap.xml (and nested sitemap indexes) for a domain.
  2. Merge with manually-added extra_urls and robots.txt-declared sitemaps.
  3. Fetch each URL's HTML from the origin (backend_url).
  4. Send text nodes to DeepL for each configured target language.
  5. Persist translated HTML in TranslatedPage rows (upsert).
  6. Schedule the next crawl based on CrawlFrequency.

Called by:
  - The APScheduler background job (automatic, per frequency).
  - POST /translation/{domain}/crawl-now (manual trigger via API).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Generator
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree as ET

import httpx

logger = logging.getLogger("seo_crawler")

# ── Frequency → timedelta ─────────────────────────────────────────────────────

_FREQ_DELTA = {
    "hourly": timedelta(hours=1),
    "daily":  timedelta(days=1),
    "weekly": timedelta(weeks=1),
    "manual": timedelta(days=365 * 10),   # effectively never auto-runs
}


def next_crawl_time(frequency: str) -> datetime:
    delta = _FREQ_DELTA.get(frequency, timedelta(days=1))
    return datetime.now(timezone.utc) + delta


# ── robots.txt parser ─────────────────────────────────────────────────────────

def _sitemap_urls_from_robots(robots_txt: str, base_url: str) -> list[str]:
    """Extract Sitemap: directives from robots.txt."""
    urls = []
    for line in robots_txt.splitlines():
        line = line.strip()
        if line.lower().startswith("sitemap:"):
            raw = line.split(":", 1)[1].strip()
            urls.append(urljoin(base_url, raw))
    return urls


# ── Sitemap parser ────────────────────────────────────────────────────────────

_NS = {
    "sm":  "http://www.sitemaps.org/schemas/sitemap/0.9",
    "xhtml": "http://www.w3.org/1999/xhtml",
    "image": "http://www.google.com/schemas/sitemap-image/1.1",
    "news":  "http://www.google.com/schemas/sitemap-news/0.9",
}

def _iter_sitemap_urls(xml_text: str, base_url: str) -> Generator[str, None, None]:
    """
    Yield all <loc> values from a sitemap or sitemap-index document.
    Handles both flat sitemaps and sitemap indexes (recursive would
    be done by the caller).
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.warning("Sitemap XML parse error at %s: %s", base_url, exc)
        return

    tag = root.tag.lower()
    # Sitemap index → yield child sitemap locs
    if "sitemapindex" in tag:
        for sm in root.iter():
            if sm.tag.endswith("}loc") or sm.tag == "loc":
                loc = sm.text.strip() if sm.text else ""
                if loc:
                    yield urljoin(base_url, loc)
    # Regular sitemap → yield page locs
    else:
        for url_el in root.iter():
            if url_el.tag.endswith("}loc") or url_el.tag == "loc":
                loc = url_el.text.strip() if url_el.text else ""
                if loc:
                    yield urljoin(base_url, loc)


async def _fetch_text(client: httpx.AsyncClient, url: str) -> tuple[int, str]:
    """Fetch URL, return (status_code, text). Never raises."""
    try:
        r = await client.get(url, follow_redirects=True, timeout=15)
        return r.status_code, r.text
    except Exception as exc:
        logger.warning("Fetch failed %s: %s", url, exc)
        return 0, ""


async def discover_urls(backend_url: str, extra_urls: list[str]) -> list[str]:
    """
    Return the de-duplicated list of page URLs to translate for a domain.

    Steps:
      1. Fetch /robots.txt → extract Sitemap: directives.
      2. Fetch /sitemap.xml as fallback.
      3. For each sitemap URL: if it looks like a sitemap index, recurse one level.
      4. Merge all discovered URLs with extra_urls.
    """
    base = backend_url.rstrip("/")
    found: set[str] = set(extra_urls)
    sitemap_candidates: list[str] = []

    async with httpx.AsyncClient(verify=False, timeout=15) as client:
        # robots.txt
        status, robots_text = await _fetch_text(client, f"{base}/robots.txt")
        if status == 200 and robots_text:
            sm_from_robots = _sitemap_urls_from_robots(robots_text, base)
            sitemap_candidates.extend(sm_from_robots)
            logger.info("robots.txt: found %d sitemap(s)", len(sm_from_robots))

        # fallback
        if not sitemap_candidates:
            sitemap_candidates.append(f"{base}/sitemap.xml")
            sitemap_candidates.append(f"{base}/sitemap_index.xml")

        # fetch each sitemap
        visited_sitemaps: set[str] = set()
        page_urls: set[str] = set()

        async def _process_sitemap(sm_url: str, depth: int = 0) -> None:
            if sm_url in visited_sitemaps or depth > 3:
                return
            visited_sitemaps.add(sm_url)
            st, xml = await _fetch_text(client, sm_url)
            if st != 200 or not xml.strip():
                return
            for loc in _iter_sitemap_urls(xml, base):
                # if loc itself looks like a sitemap → recurse
                if loc.endswith(".xml") and "sitemap" in loc.lower():
                    await _process_sitemap(loc, depth + 1)
                else:
                    page_urls.add(loc)

        for sm_url in sitemap_candidates:
            await _process_sitemap(sm_url)

    all_urls = page_urls | found
    logger.info("discover_urls(%s): %d URLs total", base, len(all_urls))
    return list(all_urls)


# ── HTML translation (reuse worker logic inline) ──────────────────────────────

from bs4 import BeautifulSoup, NavigableString, Comment, Tag

_SKIP_TAGS: frozenset[str] = frozenset(
    {"script", "style", "code", "pre", "textarea", "noscript",
     "template", "svg", "math"}
)
_TRANSLATE_ATTRS: frozenset[str] = frozenset(
    {"alt", "title", "placeholder", "aria-label"}
)


def _visible_text_nodes(soup: BeautifulSoup) -> list[NavigableString]:
    nodes: list[NavigableString] = []
    def _walk(tag: Tag) -> None:
        for child in tag.children:
            if isinstance(child, Comment):
                continue
            if isinstance(child, NavigableString):
                if child.strip():
                    nodes.append(child)
            elif isinstance(child, Tag):
                if child.name not in _SKIP_TAGS:
                    _walk(child)
    _walk(soup)
    return nodes


def _attr_nodes(soup: BeautifulSoup) -> list[tuple[Tag, str]]:
    pairs = []
    for tag in soup.find_all(True):
        for attr in _TRANSLATE_ATTRS:
            if tag.get(attr, "").strip():
                pairs.append((tag, attr))
    return pairs


async def translate_html(
    html_bytes: bytes,
    encoding: str,
    target_lang: str,
    deepl_api_key: str,
    max_chars: int = 100_000,
) -> str:
    """Translate all visible text in HTML and return translated HTML string."""
    html = html_bytes.decode(encoding, errors="replace")
    soup = BeautifulSoup(html, "html.parser")

    text_nodes = _visible_text_nodes(soup)
    attr_nodes = _attr_nodes(soup)
    texts      = [str(n) for n in text_nodes]
    attr_texts = [tag[attr] for tag, attr in attr_nodes]
    all_texts  = texts + attr_texts

    if not all_texts:
        return html

    translated = await _call_deepl(all_texts, target_lang, deepl_api_key, max_chars)

    for node, new_text in zip(text_nodes, translated[: len(texts)]):
        node.replace_with(NavigableString(new_text))
    for (tag, attr), new_val in zip(attr_nodes, translated[len(texts):]):
        tag[attr] = new_val

    return str(soup)


async def _call_deepl(
    texts: list[str],
    target_lang: str,
    api_key: str,
    max_chars: int,
) -> list[str]:
    if not api_key:
        return texts

    base = (
        "https://api-free.deepl.com/v2"
        if api_key.endswith(":fx")
        else "https://api.deepl.com/v2"
    )

    # Trim to budget
    budget  = max_chars
    trimmed = []
    for t in texts:
        if len(t) > budget:
            break
        trimmed.append(t)
        budget -= len(t)

    results = list(texts)
    if not trimmed:
        return results

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{base}/translate",
            json={
                "text": trimmed,
                "target_lang": target_lang,
                "tag_handling": "xml",
                "split_sentences": "nonewlines",
            },
            headers={
                "Authorization": f"DeepL-Auth-Key {api_key}",
                "Content-Type": "application/json",
            },
        )
        resp.raise_for_status()
        for i, item in enumerate(resp.json()["translations"]):
            results[i] = item["text"]

    return results


# ── Full crawl job ────────────────────────────────────────────────────────────

async def run_crawl(
    config_id: str,
    domain: str,
    backend_url: str,
    languages: list[str],
    extra_urls: list[str],
    deepl_api_key: str,
    db_session_factory,          # callable → SQLAlchemy Session
) -> dict:
    """
    Main entry point called by the scheduler or API.
    Returns a summary dict with counts.
    """
    from .models import TranslationConfig, TranslatedPage, CrawlStatus
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    import uuid as _uuid

    logger.info("Crawl start: domain=%s langs=%s", domain, languages)
    summary = {"domain": domain, "urls": 0, "ok": 0, "failed": 0, "langs": languages}

    urls = await discover_urls(backend_url, extra_urls)
    summary["urls"] = len(urls)

    if not urls:
        logger.warning("No URLs discovered for %s", domain)
        return summary

    db = db_session_factory()
    try:
        async with httpx.AsyncClient(verify=False, timeout=20) as client:
            for url in urls:
                for lang in languages:
                    try:
                        status, content_type, html_bytes = await _fetch_page(client, url)
                        if status != 200 or b"text/html" not in content_type.lower().encode():
                            _upsert_page(db, config_id, url, lang,
                                         None, CrawlStatus.FAILED,
                                         f"HTTP {status} / {content_type}", str(status))
                            summary["failed"] += 1
                            continue

                        # detect encoding
                        encoding = "utf-8"
                        for part in content_type.split(";"):
                            if "charset=" in part:
                                encoding = part.split("=", 1)[1].strip()

                        translated = await translate_html(
                            html_bytes, encoding, lang, deepl_api_key
                        )
                        _upsert_page(db, config_id, url, lang,
                                     translated, CrawlStatus.DONE, None, str(status))
                        summary["ok"] += 1
                        logger.debug("Translated %s → %s", url, lang)

                    except Exception as exc:
                        logger.error("Error translating %s (%s): %s", url, lang, exc)
                        _upsert_page(db, config_id, url, lang,
                                     None, CrawlStatus.FAILED, str(exc), None)
                        summary["failed"] += 1

        # Update config timestamps
        cfg = db.query(TranslationConfig).filter(
            TranslationConfig.id == config_id
        ).first()
        if cfg:
            cfg.last_crawl = datetime.now(timezone.utc)
            cfg.updated_at = datetime.now(timezone.utc)
            db.commit()

    finally:
        db.close()

    logger.info("Crawl done: %s", summary)
    return summary


async def _fetch_page(
    client: httpx.AsyncClient, url: str
) -> tuple[int, str, bytes]:
    try:
        r = await client.get(url, follow_redirects=True, timeout=20)
        ct = r.headers.get("content-type", "")
        return r.status_code, ct, r.content
    except Exception as exc:
        logger.warning("Page fetch failed %s: %s", url, exc)
        return 0, "", b""


def _upsert_page(db, config_id, url, lang, html, status, error, origin_status):
    """Insert or update a TranslatedPage row."""
    from .models import TranslatedPage
    row = (
        db.query(TranslatedPage)
        .filter_by(config_id=config_id, url=url, language=lang)
        .first()
    )
    now = datetime.now(timezone.utc)
    if row:
        row.html          = html
        row.status        = status
        row.error         = error
        row.origin_status = origin_status
        row.crawled_at    = now
        row.updated_at    = now
    else:
        import uuid as _uuid
        row = TranslatedPage(
            id=_uuid.uuid4(),
            config_id=config_id,
            url=url,
            language=lang,
            html=html,
            status=status,
            error=error,
            origin_status=origin_status,
            crawled_at=now,
        )
        db.add(row)
    db.commit()
