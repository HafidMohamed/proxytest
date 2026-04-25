"""
Translation Worker  (v2)
=========================

Changes vs v1:
  - Reads X-Translate-Lang per-request (multi-language support via nginx)
  - Checks pre-computed DB cache first (served by control-plane)
  - CDN redirect support: if control-plane returns a 302, follow it
  - Injects language switcher JS via query to control-plane for live translations
  - Robust hop-by-hop header stripping
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
)
logger = logging.getLogger("translation_worker")

# ── Config ────────────────────────────────────────────────────────────────────

DEEPL_API_KEY:   str   = os.environ.get("DEEPL_API_KEY", "")
TARGET_LANG:     str   = os.environ.get("TARGET_LANG", "DE").upper()
MAX_CHARS:       int   = int(os.environ.get("MAX_CHARS_PER_REQUEST", "100000"))
HTTPX_TIMEOUT:   float = float(os.environ.get("HTTPX_TIMEOUT", "20"))
DEEPL_TIMEOUT:   float = float(os.environ.get("DEEPL_TIMEOUT", "30"))
CACHE_TTL:       int   = int(os.environ.get("CACHE_TTL", "300"))
CACHE_MAX_PAGES: int   = int(os.environ.get("CACHE_MAX_PAGES", "500"))
CONTROL_PLANE_URL: str = os.environ.get("CONTROL_PLANE_URL", "http://127.0.0.1:8000")

_DEEPL_BASE = (
    "https://api-free.deepl.com/v2"
    if DEEPL_API_KEY.endswith(":fx")
    else "https://api.deepl.com/v2"
)

_SKIP_TAGS: frozenset[str] = frozenset(
    {"script", "style", "code", "pre", "textarea", "noscript", "template", "svg", "math"}
)
_TRANSLATE_ATTRS: frozenset[str] = frozenset(
    {"alt", "title", "placeholder", "aria-label", "data-label"}
)

# ── LRU cache ─────────────────────────────────────────────────────────────────

class _LRUCache:
    def __init__(self, maxsize: int, ttl: int) -> None:
        self._store: OrderedDict[str, tuple[float, bytes]] = OrderedDict()
        self._maxsize = maxsize
        self._ttl = ttl

    def _key(self, url: str, lang: str) -> str:
        return hashlib.sha256(f"{lang}:{url}".encode()).hexdigest()

    def get(self, url: str, lang: str) -> bytes | None:
        k = self._key(url, lang)
        if k not in self._store:
            return None
        ts, data = self._store[k]
        if time.monotonic() - ts > self._ttl:
            del self._store[k]
            return None
        self._store.move_to_end(k)
        return data

    def set(self, url: str, lang: str, data: bytes) -> None:
        k = self._key(url, lang)
        self._store[k] = (time.monotonic(), data)
        self._store.move_to_end(k)
        while len(self._store) > self._maxsize:
            self._store.popitem(last=False)


_cache = _LRUCache(maxsize=CACHE_MAX_PAGES, ttl=CACHE_TTL)

# ── DeepL client ──────────────────────────────────────────────────────────────

async def _translate_texts(texts: list[str], target_lang: str) -> list[str]:
    if not texts or not DEEPL_API_KEY:
        return texts

    budget, trimmed = MAX_CHARS, []
    for t in texts:
        if len(t) > budget:
            break
        trimmed.append(t)
        budget -= len(t)

    results = list(texts)

    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=DEEPL_TIMEOUT) as client:
                resp = await client.post(
                    f"{_DEEPL_BASE}/translate",
                    json={"text": trimmed, "target_lang": target_lang,
                          "tag_handling": "xml", "split_sentences": "nonewlines"},
                    headers={"Authorization": f"DeepL-Auth-Key {DEEPL_API_KEY}",
                             "Content-Type": "application/json"},
                )
                if resp.status_code in (429,) or resp.status_code >= 500:
                    await asyncio.sleep(2 ** attempt)
                    continue
                resp.raise_for_status()
                for i, item in enumerate(resp.json()["translations"]):
                    results[i] = item["text"]
                return results
        except Exception as exc:
            logger.warning("DeepL attempt %d failed: %s", attempt + 1, exc)
            if attempt < 2:
                await asyncio.sleep(2 ** attempt)

    return results


# ── HTML rewriting ────────────────────────────────────────────────────────────

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


async def _rewrite_html(html_bytes: bytes, encoding: str, lang: str) -> bytes:
    html  = html_bytes.decode(encoding, errors="replace")
    soup  = BeautifulSoup(html, "html.parser")
    text_nodes = _visible_text_nodes(soup)
    attr_nodes = _attr_nodes(soup)
    texts      = [str(n) for n in text_nodes]
    attr_texts = [tag[attr] for tag, attr in attr_nodes]
    all_texts  = texts + attr_texts

    if not all_texts:
        return html_bytes

    try:
        all_translated = await _translate_texts(all_texts, lang)
    except Exception as exc:
        logger.error("DeepL translation failed: %s", exc)
        return html_bytes

    for node, new_text in zip(text_nodes, all_translated[:len(texts)]):
        node.replace_with(NavigableString(new_text))
    for (tag, attr), new_val in zip(attr_nodes, all_translated[len(texts):]):
        tag[attr] = new_val

    return str(soup).encode(encoding)


# ── Hop-by-hop headers ────────────────────────────────────────────────────────

_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
    "x-upstream-url", "x-translate-lang",
})

# ── Origin fetch ──────────────────────────────────────────────────────────────

async def _fetch_origin(upstream_url, method, headers, body) -> httpx.Response:
    fwd = {k: v for k, v in headers.items() if k.lower() not in _HOP_BY_HOP}
    async with httpx.AsyncClient(timeout=HTTPX_TIMEOUT, follow_redirects=True, verify=False) as client:
        req = client.build_request(method, upstream_url, headers=fwd, content=body)
        return await client.send(req)


# ── Pre-computed page lookup ──────────────────────────────────────────────────

async def _lookup_precomputed(domain: str, lang: str, url: str) -> str | None:
    if not CONTROL_PLANE_URL or not domain:
        return None
    from urllib.parse import urlparse
    parsed = urlparse(url)
    path   = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    lookup_url = f"{CONTROL_PLANE_URL}/translated/{domain}/{lang.upper()}/{path.lstrip('/')}"
    try:
        async with httpx.AsyncClient(timeout=2, follow_redirects=True) as client:
            resp = await client.get(lookup_url)
            if resp.status_code == 200:
                ct = resp.headers.get("content-type", "")
                if "text/html" in ct:
                    return resp.text
    except Exception as exc:
        logger.debug("Pre-computed lookup failed: %s", exc)
    return None


# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="Translation Worker",
    version="2.0.0",
    docs_url="/worker-docs",
)


@app.get("/worker-health")
async def health() -> dict:
    return {
        "status": "ok",
        "deepl_configured": bool(DEEPL_API_KEY),
        "default_lang": TARGET_LANG,
        "cache_size": len(_cache._store),
    }


@app.get("/robots.txt", include_in_schema=False)
async def robots_txt(request: Request) -> Response:
    domain = request.headers.get("x-original-host", "")
    if domain and CONTROL_PLANE_URL:
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                r = await client.get(f"{CONTROL_PLANE_URL}/translated/{domain}/robots.txt")
                if r.status_code == 200:
                    return Response(content=r.content, media_type="text/plain")
        except Exception:
            pass
    upstream_url = request.headers.get("x-upstream-url", "")
    if upstream_url:
        try:
            async with httpx.AsyncClient(timeout=5, verify=False) as client:
                r = await client.get(upstream_url)
                return Response(content=r.content, media_type="text/plain")
        except Exception:
            pass
    return Response(content=b"User-agent: *\nAllow: /\n", media_type="text/plain")


@app.get("/sitemap-{lang}.xml", include_in_schema=False)
async def language_sitemap(request: Request, lang: str) -> Response:
    domain = request.headers.get("x-original-host", "")
    if domain and CONTROL_PLANE_URL:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(
                    f"{CONTROL_PLANE_URL}/translated/{domain}/{lang.upper()}/sitemap.xml"
                )
                if r.status_code == 200:
                    return Response(content=r.content, media_type="application/xml")
        except Exception:
            pass
    return Response(
        content=b'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"/>',
        media_type="application/xml",
    )


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
async def proxy(request: Request, path: str) -> Response:
    upstream_url: str | None = request.headers.get("x-upstream-url")
    if not upstream_url:
        return Response(content=b"Missing X-Upstream-URL header", status_code=400)

    # Per-request language (set by nginx from the URL prefix or vhost)
    lang = request.headers.get("x-translate-lang", TARGET_LANG).upper()

    # ── 1. Pre-computed DB/CDN cache ─────────────────────────────────────────
    if request.method == "GET":
        domain_header = request.headers.get("x-original-host", "")
        if domain_header:
            precomputed = await _lookup_precomputed(domain_header, lang, upstream_url)
            if precomputed is not None:
                return Response(
                    content=precomputed.encode("utf-8"),
                    media_type="text/html; charset=utf-8",
                    headers={"X-Translation-Cache": "PRECOMPUTED", "X-Translated-To": lang},
                )

    # ── 2. In-memory LRU cache ────────────────────────────────────────────────
    if request.method == "GET":
        cached = _cache.get(upstream_url, lang)
        if cached is not None:
            return Response(
                content=cached, media_type="text/html; charset=utf-8",
                headers={"X-Translation-Cache": "HIT", "X-Translated-To": lang},
            )

    # ── 3. Fetch origin ───────────────────────────────────────────────────────
    body = await request.body()
    try:
        origin_resp = await _fetch_origin(
            upstream_url=upstream_url,
            method=request.method,
            headers=dict(request.headers),
            body=body or None,
        )
    except httpx.TimeoutException:
        return Response(content=b"Origin timeout", status_code=504)
    except httpx.RequestError as exc:
        logger.error("Origin request error: %s – %s", upstream_url, exc)
        return Response(content=b"Bad gateway", status_code=502)

    content_type = origin_resp.headers.get("content-type", "")
    is_html = "text/html" in content_type

    resp_headers = {
        k: v for k, v in origin_resp.headers.items()
        if k.lower() not in _HOP_BY_HOP and k.lower() != "content-length"
    }
    resp_headers["X-Translated-To"] = lang if is_html else "none"

    if not is_html:
        async def _passthrough() -> AsyncIterator[bytes]:
            async for chunk in origin_resp.aiter_bytes(chunk_size=65536):
                yield chunk
        return StreamingResponse(_passthrough(), status_code=origin_resp.status_code,
                                 headers=resp_headers, media_type=content_type)

    # ── 4. Translate HTML ─────────────────────────────────────────────────────
    raw_html = await origin_resp.aread()
    encoding = "utf-8"
    for part in content_type.split(";"):
        if "charset=" in part:
            encoding = part.split("=", 1)[1].strip()
            break

    translated_html = await _rewrite_html(raw_html, encoding, lang)

    if request.method == "GET":
        _cache.set(upstream_url, lang, translated_html)

    resp_headers["X-Translation-Cache"] = "MISS"
    resp_headers["content-type"]        = f"text/html; charset={encoding}"

    async def _stream() -> AsyncIterator[bytes]:
        chunk_size = 65536
        for i in range(0, len(translated_html), chunk_size):
            yield translated_html[i:i + chunk_size]
            await asyncio.sleep(0)

    return StreamingResponse(_stream(), status_code=origin_resp.status_code,
                             headers=resp_headers, media_type=f"text/html; charset={encoding}")
