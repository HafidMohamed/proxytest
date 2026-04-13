"""
Translation Worker
==================

Sits between Nginx and the customer's origin. When Nginx forwards an incoming
request here, this service:

  1. Fetches the origin URL (passed via X-Upstream-URL header).
  2. If the response is HTML, extracts all visible text nodes with BeautifulSoup.
  3. Sends the text batch to DeepL in one API call (respecting character limits).
  4. Splices the translated strings back into the DOM.
  5. Streams the rewritten HTML back to Nginx.

For non-HTML responses (images, JSON, CSS, …) the bytes are piped through
unchanged.

Traffic flow
------------
  Visitor
    → Nginx (TLS termination, Cloudflare IP check)
        → translation_worker (this service, port 8001)
            → customer's origin (backend_url)
        ← translated HTML
    ← visitor browser

Configuration (env vars / .env)
---------------------------------
  DEEPL_API_KEY          DeepL Free or Pro key  (required)
  WORKER_HOST            0.0.0.0
  WORKER_PORT            8001
  TARGET_LANG            DE   (BCP-47 language code, uppercase)
  MAX_CHARS_PER_REQUEST  100000  (DeepL free tier: 500k/month total)
  HTTPX_TIMEOUT          20     (seconds, for upstream fetch)
  DEEPL_TIMEOUT          30     (seconds, for DeepL API call)
  CACHE_TTL              300    (seconds to keep translated pages in RAM cache)
  CACHE_MAX_PAGES        500    (LRU page limit)
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from collections import OrderedDict
from typing import AsyncIterator

import httpx
from bs4 import BeautifulSoup, NavigableString, Tag, Comment
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
)
logger = logging.getLogger("translation_worker")

# ── Config ────────────────────────────────────────────────────────────────────

DEEPL_API_KEY: str = os.environ.get("DEEPL_API_KEY", "")
TARGET_LANG: str = os.environ.get("TARGET_LANG", "DE").upper()
MAX_CHARS: int = int(os.environ.get("MAX_CHARS_PER_REQUEST", "100000"))
HTTPX_TIMEOUT: float = float(os.environ.get("HTTPX_TIMEOUT", "20"))
DEEPL_TIMEOUT: float = float(os.environ.get("DEEPL_TIMEOUT", "30"))
CACHE_TTL: int = int(os.environ.get("CACHE_TTL", "300"))
CACHE_MAX_PAGES: int = int(os.environ.get("CACHE_MAX_PAGES", "500"))

# DeepL endpoint: Free keys end with ":fx", Pro keys do not.
_DEEPL_BASE = (
    "https://api-free.deepl.com/v2"
    if DEEPL_API_KEY.endswith(":fx")
    else "https://api.deepl.com/v2"
)

# ── Tags we must NOT translate ────────────────────────────────────────────────

_SKIP_TAGS: frozenset[str] = frozenset(
    {
        "script",
        "style",
        "code",
        "pre",
        "textarea",
        "noscript",
        "template",
        "svg",
        "math",
    }
)

# HTML attributes that may contain translatable text
_TRANSLATE_ATTRS: frozenset[str] = frozenset({"alt", "title", "placeholder", "aria-label"})

# ── In-memory LRU cache ───────────────────────────────────────────────────────

class _LRUCache:
    """Thread-safe LRU cache (asyncio-safe via GIL — no locks needed)."""

    def __init__(self, maxsize: int, ttl: int) -> None:
        self._store: OrderedDict[str, tuple[float, bytes, str]] = OrderedDict()
        self._maxsize = maxsize
        self._ttl = ttl

    def _key(self, url: str, lang: str) -> str:
        return hashlib.sha256(f"{lang}:{url}".encode()).hexdigest()

    def get(self, url: str, lang: str) -> bytes | None:
        k = self._key(url, lang)
        if k not in self._store:
            return None
        ts, data, _ = self._store[k]
        if time.monotonic() - ts > self._ttl:
            del self._store[k]
            return None
        self._store.move_to_end(k)
        return data

    def set(self, url: str, lang: str, data: bytes) -> None:
        k = self._key(url, lang)
        self._store[k] = (time.monotonic(), data, url)
        self._store.move_to_end(k)
        while len(self._store) > self._maxsize:
            self._store.popitem(last=False)


_cache = _LRUCache(maxsize=CACHE_MAX_PAGES, ttl=CACHE_TTL)

# ── DeepL client ──────────────────────────────────────────────────────────────

async def _translate_texts(texts: list[str], target_lang: str) -> list[str]:
    """
    Send up to MAX_CHARS characters to DeepL in one request.
    Returns translated strings in the same order.
    DeepL preserves HTML tags when tag_handling="html" — we pass raw text, so
    we use "xml" to protect any stray ampersands / entities.
    """
    if not texts:
        return []
    if not DEEPL_API_KEY:
        logger.warning("DEEPL_API_KEY not set – returning originals unchanged")
        return texts

    # Trim texts that exceed character budget
    budget = MAX_CHARS
    trimmed: list[str] = []
    for t in texts:
        if len(t) > budget:
            break
        trimmed.append(t)
        budget -= len(t)

    # Fall back for texts exceeding budget
    results: list[str] = list(texts)

    async with httpx.AsyncClient(timeout=DEEPL_TIMEOUT) as client:
        payload = {
            "text": trimmed,
            "target_lang": target_lang,
            "tag_handling": "xml",
            "split_sentences": "nonewlines",
        }
        headers = {
            "Authorization": f"DeepL-Auth-Key {DEEPL_API_KEY}",
            "Content-Type": "application/json",
        }
        resp = await client.post(f"{_DEEPL_BASE}/translate", json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        translated = [item["text"] for item in data["translations"]]
        for i, t in enumerate(translated):
            results[i] = t

    return results


# ── HTML rewriting ────────────────────────────────────────────────────────────

def _visible_text_nodes(soup: BeautifulSoup) -> list[NavigableString]:
    """
    Yield all NavigableString nodes that:
      - are not inside a skipped tag
      - are not HTML comments
      - contain at least one non-whitespace character
    """
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
    """Return (tag, attr_name) pairs for translatable HTML attributes."""
    pairs: list[tuple[Tag, str]] = []
    for tag in soup.find_all(True):
        for attr in _TRANSLATE_ATTRS:
            val = tag.get(attr, "")
            if val and val.strip():
                pairs.append((tag, attr))
    return pairs


async def _rewrite_html(html_bytes: bytes, encoding: str) -> bytes:
    """Parse, translate all visible text, return rewritten HTML bytes."""
    html = html_bytes.decode(encoding, errors="replace")
    soup = BeautifulSoup(html, "html.parser")

    # ── Collect text nodes ────────────────────────────────────────────────────
    text_nodes = _visible_text_nodes(soup)
    attr_nodes = _attr_nodes(soup)

    texts = [str(n) for n in text_nodes]
    attr_texts = [tag[attr] for tag, attr in attr_nodes]
    all_texts = texts + attr_texts

    if not all_texts:
        return html_bytes

    # ── Translate ─────────────────────────────────────────────────────────────
    try:
        all_translated = await _translate_texts(all_texts, TARGET_LANG)
    except Exception as exc:
        logger.error("DeepL translation failed: %s", exc)
        # Return original on translation error — don't break the page
        return html_bytes

    # ── Splice back ───────────────────────────────────────────────────────────
    translated_texts = all_translated[: len(texts)]
    translated_attrs = all_translated[len(texts) :]

    for node, new_text in zip(text_nodes, translated_texts):
        node.replace_with(NavigableString(new_text))

    for (tag, attr), new_val in zip(attr_nodes, translated_attrs):
        tag[attr] = new_val

    return str(soup).encode(encoding)


# ── Origin fetch ──────────────────────────────────────────────────────────────

_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        # Our own routing headers — must not be forwarded to origin
        "x-upstream-url",
        "x-translate-lang",
    }
)


async def _fetch_origin(
    upstream_url: str,
    method: str,
    headers: dict[str, str],
    body: bytes | None,
) -> httpx.Response:
    """Forward the request to the origin and return the raw response."""
    # Strip hop-by-hop and our internal routing headers
    fwd_headers = {
        k: v for k, v in headers.items() if k.lower() not in _HOP_BY_HOP
    }
    async with httpx.AsyncClient(
        timeout=HTTPX_TIMEOUT,
        follow_redirects=True,
        verify=False,  # mirrors nginx proxy_ssl_verify off
    ) as client:
        req = client.build_request(
            method,
            upstream_url,
            headers=fwd_headers,
            content=body,
        )
        return await client.send(req)


# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="Translation Worker",
    version="1.0.0",
    description="HTML translation middleware — called by Nginx for every customer request.",
    docs_url="/worker-docs",  # keep /docs free for the control plane
)


@app.get("/worker-health")
async def health() -> dict:
    deepl_configured = bool(DEEPL_API_KEY)
    return {
        "status": "ok",
        "deepl_configured": deepl_configured,
        "target_lang": TARGET_LANG,
        "cache_size": len(_cache._store),
    }


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
async def proxy(request: Request, path: str) -> Response:
    """
    Main proxy handler.

    Nginx must send these two headers:
      X-Upstream-URL    Full URL to fetch from origin (e.g. https://origin.com/page)
      X-Translate-Lang  Target language code (optional, falls back to TARGET_LANG)
    """
    upstream_url: str | None = request.headers.get("x-upstream-url")
    if not upstream_url:
        return Response(
            content=b"Missing X-Upstream-URL header",
            status_code=400,
        )

    lang = request.headers.get("x-translate-lang", TARGET_LANG).upper()

    # ── Cache lookup (GET only) ───────────────────────────────────────────────
    if request.method == "GET":
        cached = _cache.get(upstream_url, lang)
        if cached is not None:
            logger.debug("Cache hit: %s", upstream_url)
            return Response(
                content=cached,
                media_type="text/html; charset=utf-8",
                headers={"X-Translation-Cache": "HIT", "X-Translated-To": lang},
            )

    # ── Fetch origin ─────────────────────────────────────────────────────────
    body = await request.body()
    try:
        origin_resp = await _fetch_origin(
            upstream_url=upstream_url,
            method=request.method,
            headers=dict(request.headers),
            body=body or None,
        )
    except httpx.TimeoutException:
        logger.warning("Origin timeout: %s", upstream_url)
        return Response(content=b"Origin timeout", status_code=504)
    except httpx.RequestError as exc:
        logger.error("Origin request error: %s – %s", upstream_url, exc)
        return Response(content=b"Bad gateway", status_code=502)

    # ── Pass non-HTML through unchanged ──────────────────────────────────────
    content_type = origin_resp.headers.get("content-type", "")
    is_html = "text/html" in content_type

    # Build response headers (strip hop-by-hop)
    resp_headers = {
        k: v
        for k, v in origin_resp.headers.items()
        if k.lower() not in _HOP_BY_HOP
        # Remove content-length — rewriting will change the body size
        and k.lower() != "content-length"
    }
    resp_headers["X-Translated-To"] = lang if is_html else "none"

    if not is_html:
        # Stream non-HTML bytes directly back
        async def _passthrough() -> AsyncIterator[bytes]:
            async for chunk in origin_resp.aiter_bytes(chunk_size=65536):
                yield chunk

        return StreamingResponse(
            _passthrough(),
            status_code=origin_resp.status_code,
            headers=resp_headers,
            media_type=content_type,
        )

    # ── Translate HTML ────────────────────────────────────────────────────────
    raw_html = await origin_resp.aread()

    # Detect charset from Content-Type header or fall back to utf-8
    encoding = "utf-8"
    for part in content_type.split(";"):
        if "charset=" in part:
            encoding = part.split("=", 1)[1].strip()
            break

    translated_html = await _rewrite_html(raw_html, encoding)

    # ── Cache & return ────────────────────────────────────────────────────────
    if request.method == "GET":
        _cache.set(upstream_url, lang, translated_html)

    resp_headers["X-Translation-Cache"] = "MISS"
    resp_headers["content-type"] = f"text/html; charset={encoding}"

    async def _stream_translated() -> AsyncIterator[bytes]:
        # Send in 64KB chunks so nginx can start forwarding immediately
        chunk_size = 65536
        for i in range(0, len(translated_html), chunk_size):
            yield translated_html[i : i + chunk_size]
            await asyncio.sleep(0)  # yield control to event loop

    return StreamingResponse(
        _stream_translated(),
        status_code=origin_resp.status_code,
        headers=resp_headers,
        media_type=f"text/html; charset={encoding}",
    )
