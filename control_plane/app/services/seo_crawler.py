"""
SEO Crawler & Translator  (v2)
================================

What's new vs v1:
  - Translation Memory: texts are looked up in TM before calling DeepL.
    Only cache misses go to the API — cuts costs 60-80% on recurring content.
  - Glossary: brand terms are protected before DeepL and restored after.
  - hreflang injection: all translated <head> elements get correct
    <link rel="alternate" hreflang="..."> tags for multilingual SEO.
  - CDN storage: HTML stored in S3/R2 when CDN_BACKEND=s3 is set;
    falls back to DB column in dev mode.
  - Usage metering: word counts recorded per crawl for billing.
  - DeepL retry: exponential backoff on 429/5xx (3 retries).
  - Sitemap fix: sitemap-index vs page URLs now handled separately
    (prevents XML files from being queued as pages to translate).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Generator
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree as ET

import httpx
from bs4 import BeautifulSoup, NavigableString, Comment, Tag

logger = logging.getLogger("seo_crawler")

# ── Frequency → timedelta ──────────────────────────────────────────────────

_FREQ_DELTA = {
    "hourly": timedelta(hours=1),
    "daily":  timedelta(days=1),
    "weekly": timedelta(weeks=1),
    "manual": timedelta(days=365 * 10),
}


def next_crawl_time(frequency: str) -> datetime:
    delta = _FREQ_DELTA.get(frequency, timedelta(days=1))
    return datetime.now(timezone.utc) + delta


# ── Sitemap parsing (fixed: index vs page URLs separated) ─────────────────

_NS = {
    "sm":    "http://www.sitemaps.org/schemas/sitemap/0.9",
    "xhtml": "http://www.w3.org/1999/xhtml",
}


def _iter_sitemap_urls(xml_text: str, base_url: str) -> tuple[list[str], list[str]]:
    """
    Parse sitemap XML.
    Returns (child_sitemaps, page_urls) — correctly separated.
    """
    child_sitemaps: list[str] = []
    page_urls: list[str] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.warning("Sitemap XML parse error: %s", exc)
        return child_sitemaps, page_urls

    tag = root.tag.lower()
    if "sitemapindex" in tag:
        # Sitemap index — each <sitemap><loc> is another sitemap file
        for loc_el in root.findall(".//{http://www.sitemaps.org/schemas/sitemap/0.9}loc"):
            loc = (loc_el.text or "").strip()
            if loc:
                child_sitemaps.append(urljoin(base_url, loc))
    else:
        # Regular sitemap — each <url><loc> is a page
        for url_el in root.findall(".//{http://www.sitemaps.org/schemas/sitemap/0.9}url"):
            for loc_el in url_el.findall("{http://www.sitemaps.org/schemas/sitemap/0.9}loc"):
                loc = (loc_el.text or "").strip()
                if loc:
                    page_urls.append(urljoin(base_url, loc))
    return child_sitemaps, page_urls


def _sitemap_urls_from_robots(robots_txt: str, base_url: str) -> list[str]:
    urls = []
    for line in robots_txt.splitlines():
        line = line.strip()
        if line.lower().startswith("sitemap:"):
            raw = line.split(":", 1)[1].strip()
            urls.append(urljoin(base_url, raw))
    return urls


async def _fetch_text(client: httpx.AsyncClient, url: str) -> tuple[int, str]:
    try:
        r = await client.get(url, follow_redirects=True, timeout=15)
        return r.status_code, r.text
    except Exception as exc:
        logger.warning("Fetch failed %s: %s", url, exc)
        return 0, ""


async def discover_urls(backend_url: str, extra_urls: list[str]) -> list[str]:
    base = backend_url.rstrip("/")
    found: set[str] = set(extra_urls)
    sitemap_candidates: list[str] = []

    # NOTE: verify=False only used because backend_url may be self-signed internal origin.
    # Production deployments with public backend should use verify=True.
    async with httpx.AsyncClient(verify=False, timeout=15) as client:
        status, robots_text = await _fetch_text(client, f"{base}/robots.txt")
        if status == 200 and robots_text:
            sm_from_robots = _sitemap_urls_from_robots(robots_text, base)
            sitemap_candidates.extend(sm_from_robots)

        if not sitemap_candidates:
            sitemap_candidates += [f"{base}/sitemap.xml", f"{base}/sitemap_index.xml"]

        visited: set[str] = set()
        page_urls: set[str] = set()

        async def _process(sm_url: str, depth: int = 0) -> None:
            if sm_url in visited or depth > 3:
                return
            visited.add(sm_url)
            st, xml = await _fetch_text(client, sm_url)
            if st != 200 or not xml.strip():
                return
            children, pages = _iter_sitemap_urls(xml, base)
            page_urls.update(pages)
            for child in children:
                await _process(child, depth + 1)

        for sm_url in sitemap_candidates:
            await _process(sm_url)

    all_urls = page_urls | found
    logger.info("discover_urls(%s): %d URLs total", base, len(all_urls))
    return list(all_urls)


# ── HTML utilities ─────────────────────────────────────────────────────────

_SKIP_TAGS: frozenset[str] = frozenset(
    {"script", "style", "code", "pre", "textarea", "noscript",
     "template", "svg", "math"}
)
_TRANSLATE_ATTRS: frozenset[str] = frozenset(
    {"alt", "title", "placeholder", "aria-label", "data-label"}
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


def _count_words(texts: list[str]) -> int:
    """Approximate word count for billing."""
    return sum(len(t.split()) for t in texts)


# ── hreflang injection ────────────────────────────────────────────────────

def inject_hreflang(
    html: str,
    domain: str,
    current_url: str,
    all_languages: list[str],
    routing_mode: str = "subdirectory",
    original_lang: str = "en",
) -> str:
    """
    Inject <link rel="alternate" hreflang="..."> tags into <head>.

    Google requires:
      1. One tag per language variant (including the original)
      2. A x-default tag
      3. Tags on EVERY language version pointing to ALL others

    URL construction:
      subdirectory:  https://domain/de/path
      subdomain:     https://de.domain/path
    """
    soup = BeautifulSoup(html, "html.parser")
    head = soup.find("head")
    if not head:
        return html

    # Remove any stale hreflang tags (e.g. from previous crawl)
    for tag in head.find_all("link", rel="alternate"):
        if tag.get("hreflang"):
            tag.decompose()

    parsed = urlparse(current_url)
    path   = parsed.path or "/"
    if parsed.query:
        path += f"?{parsed.query}"

    def _lang_url(lang: str) -> str:
        lang_lower = lang.lower()
        if routing_mode == "subdomain":
            return f"https://{lang_lower}.{domain}{path}"
        else:
            return f"https://{domain}/{lang_lower}{path}"

    def _orig_url() -> str:
        return f"https://{domain}{path}"

    def _make_link(hreflang: str, href: str) -> Tag:
        tag = soup.new_tag("link", rel="alternate", hreflang=hreflang, href=href)
        return tag

    # Add original language tag
    head.append(_make_link(original_lang, _orig_url()))

    # Add translated language tags
    for lang in all_languages:
        # DeepL codes are uppercase (DE, FR) but hreflang uses lowercase (de, fr)
        head.append(_make_link(lang.lower(), _lang_url(lang)))

    # x-default points to the original
    head.append(_make_link("x-default", _orig_url()))

    return str(soup)


def inject_language_switcher(html: str, domain: str, current_lang: str,
                              all_languages: list[str], routing_mode: str = "subdirectory",
                              current_path: str = "/") -> str:
    """
    Inject a minimal floating language-switcher widget into <body>.
    The widget renders as a small fixed-position select dropdown that:
      - Shows the current language
      - Lets users navigate to any translated version
      - Uses JS to redirect to the correct subdirectory/subdomain URL
    Style is intentionally minimal so it adapts to any site theme.
    """
    soup = BeautifulSoup(html, "html.parser")
    body = soup.find("body")
    if not body:
        return html

    # Deduplicate and sort languages
    langs = sorted(set(all_languages))
    if not langs:
        return html

    # Build JS URL function based on routing mode
    if routing_mode == "subdomain":
        url_js = f"""
function getLangUrl(lang) {{
    var path = window.location.pathname + window.location.search;
    return 'https://' + lang.toLowerCase() + '.{domain}' + path;
}}
function getOrigUrl() {{
    var path = window.location.pathname + window.location.search;
    return 'https://{domain}' + path;
}}"""
    else:
        url_js = f"""
function getLangUrl(lang) {{
    var path = window.location.pathname;
    // strip existing lang prefix if present
    var parts = path.split('/').filter(Boolean);
    var knownLangs = {str([l.lower() for l in langs])};
    if (parts.length > 0 && knownLangs.indexOf(parts[0]) !== -1) {{
        parts.shift();
    }}
    return 'https://{domain}/' + lang.toLowerCase() + '/' + parts.join('/') + window.location.search;
}}
function getOrigUrl() {{
    var path = window.location.pathname;
    var parts = path.split('/').filter(Boolean);
    var knownLangs = {str([l.lower() for l in langs])};
    if (parts.length > 0 && knownLangs.indexOf(parts[0]) !== -1) {{
        parts.shift();
    }}
    return 'https://{domain}/' + parts.join('/') + window.location.search;
}}"""

    # Language names for display
    lang_names = {
        "DE": "🇩🇪 Deutsch", "FR": "🇫🇷 Français", "ES": "🇪🇸 Español",
        "IT": "🇮🇹 Italiano", "PT": "🇵🇹 Português", "NL": "🇳🇱 Nederlands",
        "PL": "🇵🇱 Polski",   "RU": "🇷🇺 Русский",   "JA": "🇯🇵 日本語",
        "ZH": "🇨🇳 中文",     "KO": "🇰🇷 한국어",     "AR": "🇸🇦 العربية",
        "SV": "🇸🇪 Svenska",  "DA": "🇩🇰 Dansk",     "FI": "🇫🇮 Suomi",
        "TR": "🇹🇷 Türkçe",   "CS": "🇨🇿 Čeština",   "HU": "🇭🇺 Magyar",
    }

    options_html = '<option value="__orig__">🌐 Original</option>'
    for lang in langs:
        name = lang_names.get(lang.upper(), lang.upper())
        selected = ' selected' if lang.upper() == current_lang.upper() else ''
        options_html += f'<option value="{lang}"{selected}>{name}</option>'

    switcher_html = f"""
<div id="__proxy_lang_switcher__" style="
    position:fixed; bottom:20px; right:20px; z-index:99999;
    background:#fff; border:1px solid #ddd; border-radius:8px;
    box-shadow:0 2px 12px rgba(0,0,0,.15); padding:8px 12px;
    font-family:system-ui,sans-serif; font-size:14px;
    display:flex; align-items:center; gap:8px;
">
  <span style="font-size:18px;">🌐</span>
  <select id="__proxy_lang_select__" style="
      border:none; outline:none; background:transparent;
      font-size:14px; cursor:pointer; max-width:160px;
  " onchange="__proxyLangSwitch__(this.value)">
    {options_html}
  </select>
</div>
<script>
(function() {{
  {url_js}
  window.__proxyLangSwitch__ = function(lang) {{
    var url = lang === '__orig__' ? getOrigUrl() : getLangUrl(lang);
    window.location.href = url;
  }};
}})();
</script>
"""
    switcher_tag = BeautifulSoup(switcher_html, "html.parser")
    body.append(switcher_tag)
    return str(soup)


# ── DeepL client with retry ────────────────────────────────────────────────

async def _call_deepl(
    texts: list[str],
    target_lang: str,
    api_key: str,
    max_chars: int = 100_000,
    retries: int = 3,
) -> list[str]:
    if not api_key:
        return texts

    base = (
        "https://api-free.deepl.com/v2"
        if api_key.endswith(":fx")
        else "https://api.deepl.com/v2"
    )

    budget, trimmed = max_chars, []
    for t in texts:
        if len(t) > budget:
            break
        trimmed.append(t)
        budget -= len(t)

    results = list(texts)
    if not trimmed:
        return results

    for attempt in range(retries):
        try:
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
                if resp.status_code == 429 or resp.status_code >= 500:
                    wait = 2 ** attempt
                    logger.warning("DeepL %d — retrying in %ds (attempt %d/%d)",
                                   resp.status_code, wait, attempt + 1, retries)
                    await asyncio.sleep(wait)
                    continue
                resp.raise_for_status()
                for i, item in enumerate(resp.json()["translations"]):
                    results[i] = item["text"]
                return results
        except httpx.TimeoutException:
            logger.warning("DeepL timeout (attempt %d/%d)", attempt + 1, retries)
            if attempt < retries - 1:
                await asyncio.sleep(2 ** attempt)
    logger.error("DeepL failed after %d retries", retries)
    return results


# ── Main translate_html with TM + Glossary ────────────────────────────────

async def translate_html(
    html_bytes: bytes,
    encoding: str,
    target_lang: str,
    deepl_api_key: str,
    db=None,               # SQLAlchemy session for TM + Glossary
    domain_id: str = None,
    max_chars: int = 100_000,
) -> tuple[str, int]:
    """
    Translate all visible text in HTML.

    Returns:
        (translated_html_str, words_translated_count)

    Pipeline:
      1. Parse HTML with BeautifulSoup
      2. Collect text nodes + translatable attributes
      3. Bulk-lookup texts in Translation Memory
      4. Apply Glossary protection (protect terms with placeholders)
      5. Send only TM misses to DeepL
      6. Store DeepL results back into TM
      7. Restore Glossary terms
      8. Splice all translations back into DOM
      9. Inject hreflang + language switcher (handled by run_crawl caller)
    """
    html = html_bytes.decode(encoding, errors="replace")
    soup = BeautifulSoup(html, "html.parser")

    text_nodes = _visible_text_nodes(soup)
    attr_nodes = _attr_nodes(soup)
    texts      = [str(n) for n in text_nodes]
    attr_texts = [tag[attr] for tag, attr in attr_nodes]
    all_texts  = texts + attr_texts

    if not all_texts:
        return html, 0

    # ── Translation Memory lookup ─────────────────────────────────────────
    tm_results: dict[str, str | None] = {}
    if db is not None:
        from .translation_memory import bulk_lookup
        tm_results = bulk_lookup(db, all_texts, target_lang)

    # Separate hits from misses
    miss_texts  = [t for t in all_texts if tm_results.get(t) is None]
    miss_unique = list(dict.fromkeys(miss_texts))  # deduplicated, order-preserved

    # ── Glossary protection ───────────────────────────────────────────────
    restore_map: dict[str, str] = {}
    protected_misses = miss_unique
    if db is not None and domain_id:
        from .glossary import apply_glossary, restore_texts
        protected_misses, restore_map = apply_glossary(db, miss_unique, domain_id, target_lang)

    # ── DeepL call (misses only) ──────────────────────────────────────────
    word_count = 0
    if protected_misses:
        word_count = _count_words(protected_misses)
        deepl_translations = await _call_deepl(protected_misses, target_lang, deepl_api_key, max_chars)

        # Restore glossary
        if restore_map:
            from .glossary import restore_texts
            deepl_translations = restore_texts(deepl_translations, restore_map)

        # Store back into TM
        if db is not None:
            from .translation_memory import store
            for original, translated in zip(miss_unique, deepl_translations):
                if original != translated:  # don't store untranslated
                    store(db, original, target_lang, translated)

        # Build final lookup map (TM hits + new DeepL results)
        deepl_map = dict(zip(miss_unique, deepl_translations))
    else:
        deepl_map = {}

    def _get_translation(t: str) -> str:
        if t in tm_results and tm_results[t] is not None:
            return tm_results[t]
        return deepl_map.get(t, t)

    # ── Splice back into DOM ──────────────────────────────────────────────
    for node, original in zip(text_nodes, texts):
        node.replace_with(NavigableString(_get_translation(original)))

    for (tag, attr), original in zip(attr_nodes, attr_texts):
        tag[attr] = _get_translation(original)

    return str(soup), word_count


# ── Full crawl job ─────────────────────────────────────────────────────────

async def run_crawl(
    config_id: str,
    domain: str,
    domain_id: str,
    customer_id: str,
    backend_url: str,
    languages: list[str],
    extra_urls: list[str],
    deepl_api_key: str,
    db_session_factory,
    routing_mode: str = "subdirectory",
    original_lang: str = "en",
) -> dict:
    """
    Main entry point — called by scheduler or API.

    New vs v1:
      - Passes domain_id for TM + Glossary queries
      - Passes customer_id for usage metering
      - Injects hreflang + language switcher into HTML
      - Stores HTML via cdn_storage (S3 or DB)
      - Records word usage for billing
    """
    from ..models import TranslationConfig, TranslatedPage, CrawlStatus
    from .cdn_storage import store_html
    from .usage import record_words

    logger.info("Crawl start: domain=%s langs=%s", domain, languages)
    summary = {
        "domain": domain, "urls": 0,
        "ok": 0, "failed": 0, "langs": languages,
        "words_translated": 0, "tm_hits": 0,
    }

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
                                         None, None, CrawlStatus.FAILED,
                                         f"HTTP {status} / {content_type}", str(status), 0)
                            summary["failed"] += 1
                            continue

                        encoding = "utf-8"
                        for part in content_type.split(";"):
                            if "charset=" in part:
                                encoding = part.split("=", 1)[1].strip()

                        # Translate with TM + Glossary
                        translated_html, words = await translate_html(
                            html_bytes, encoding, lang, deepl_api_key,
                            db=db, domain_id=domain_id,
                        )

                        # Inject hreflang tags (multilingual SEO)
                        translated_html = inject_hreflang(
                            translated_html, domain, url, languages,
                            routing_mode=routing_mode, original_lang=original_lang,
                        )

                        # Inject language switcher widget
                        translated_html = inject_language_switcher(
                            translated_html, domain, lang, languages,
                            routing_mode=routing_mode,
                        )

                        # Store via CDN or DB
                        inline_html, cdn_url = store_html(domain, lang, url, translated_html)

                        _upsert_page(db, config_id, url, lang,
                                     inline_html, cdn_url, CrawlStatus.DONE,
                                     None, str(status), words)

                        # Record usage for billing
                        if words > 0:
                            record_words(db, customer_id, domain_id, words, lang, url)

                        summary["ok"] += 1
                        summary["words_translated"] += words
                        logger.debug("Translated %s → %s (%d words)", url, lang, words)

                    except Exception as exc:
                        logger.error("Error translating %s (%s): %s", url, lang, exc)
                        _upsert_page(db, config_id, url, lang,
                                     None, None, CrawlStatus.FAILED, str(exc), None, 0)
                        summary["failed"] += 1

        cfg = db.query(TranslationConfig).filter(TranslationConfig.id == config_id).first()
        if cfg:
            cfg.last_crawl = datetime.now(timezone.utc)
            cfg.updated_at = datetime.now(timezone.utc)
            db.commit()

    finally:
        db.close()

    logger.info("Crawl done: %s", summary)
    return summary


async def _fetch_page(client: httpx.AsyncClient, url: str) -> tuple[int, str, bytes]:
    try:
        r = await client.get(url, follow_redirects=True, timeout=20)
        return r.status_code, r.headers.get("content-type", ""), r.content
    except Exception as exc:
        logger.warning("Page fetch failed %s: %s", url, exc)
        return 0, "", b""


def _upsert_page(db, config_id, url, lang, html, html_url, status, error, origin_status, word_count):
    from ..models import TranslatedPage
    row = db.query(TranslatedPage).filter_by(config_id=config_id, url=url, language=lang).first()
    now = datetime.now(timezone.utc)
    if row:
        row.html          = html
        row.html_url      = html_url
        row.status        = status
        row.error         = error
        row.origin_status = origin_status
        row.word_count    = word_count or 0
        row.crawled_at    = now
        row.updated_at    = now
    else:
        import uuid
        row = TranslatedPage(
            id=uuid.uuid4(), config_id=config_id, url=url, language=lang,
            html=html, html_url=html_url, status=status, error=error,
            origin_status=origin_status, word_count=word_count or 0, crawled_at=now,
        )
        db.add(row)
    db.commit()
