"""
Tests for the translation worker.

Run with:
    cd translation_worker
    pip install -r requirements.txt pytest pytest-asyncio httpx
    pytest tests/ -v
"""

from __future__ import annotations

import os
import pytest
import pytest_asyncio

# Patch env before importing app so the config picks it up
os.environ.setdefault("DEEPL_API_KEY", "test-key:fx")
os.environ.setdefault("TARGET_LANG", "DE")

from httpx import AsyncClient, ASGITransport
from unittest.mock import AsyncMock, patch

from app.main import app, _rewrite_html, _translate_texts, _LRUCache


# ── Helpers ───────────────────────────────────────────────────────────────────

SIMPLE_HTML = b"""<!DOCTYPE html>
<html>
<head><title>Hello World</title></head>
<body>
  <h1>Hello World</h1>
  <p>This is a test paragraph.</p>
  <img src="test.jpg" alt="A test image" />
  <script>var x = "do not translate";</script>
  <style>.cls { color: red; }</style>
</body>
</html>"""

TRANSLATED_TEXTS = {
    "Hello World": "Hallo Welt",
    "This is a test paragraph.": "Dies ist ein Testabsatz.",
    "A test image": "Ein Testbild",
}


def _fake_translate(texts: list[str], target_lang: str) -> list[str]:
    return [TRANSLATED_TEXTS.get(t, t) for t in texts]


# ── Unit: _rewrite_html ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rewrite_html_translates_visible_text():
    with patch("app.main._translate_texts", new=AsyncMock(side_effect=_fake_translate)):
        result = await _rewrite_html(SIMPLE_HTML, "utf-8")
    html = result.decode("utf-8")
    assert "Hallo Welt" in html
    assert "Dies ist ein Testabsatz." in html


@pytest.mark.asyncio
async def test_rewrite_html_skips_script_and_style():
    with patch("app.main._translate_texts", new=AsyncMock(side_effect=_fake_translate)):
        result = await _rewrite_html(SIMPLE_HTML, "utf-8")
    html = result.decode("utf-8")
    # Script/style content must NOT be translated
    assert 'var x = "do not translate"' in html
    assert ".cls { color: red; }" in html


@pytest.mark.asyncio
async def test_rewrite_html_translates_alt_attribute():
    with patch("app.main._translate_texts", new=AsyncMock(side_effect=_fake_translate)):
        result = await _rewrite_html(SIMPLE_HTML, "utf-8")
    html = result.decode("utf-8")
    assert 'alt="Ein Testbild"' in html


@pytest.mark.asyncio
async def test_rewrite_html_returns_original_on_deepl_error():
    async def _raise(*a, **kw):
        raise RuntimeError("DeepL unreachable")

    with patch("app.main._translate_texts", new=AsyncMock(side_effect=_raise)):
        result = await _rewrite_html(SIMPLE_HTML, "utf-8")
    # Must still return valid HTML bytes — the original
    assert b"Hello World" in result


@pytest.mark.asyncio
async def test_rewrite_html_empty_body():
    html = b"<html><body></body></html>"
    with patch("app.main._translate_texts", new=AsyncMock(side_effect=_fake_translate)):
        result = await _rewrite_html(html, "utf-8")
    assert result  # should not crash, returns something


# ── Unit: _LRUCache ───────────────────────────────────────────────────────────

def test_lru_cache_set_and_get():
    c = _LRUCache(maxsize=10, ttl=60)
    c.set("https://example.com/", "DE", b"<html>translated</html>")
    assert c.get("https://example.com/", "DE") == b"<html>translated</html>"


def test_lru_cache_miss_different_lang():
    c = _LRUCache(maxsize=10, ttl=60)
    c.set("https://example.com/", "DE", b"german")
    assert c.get("https://example.com/", "FR") is None


def test_lru_cache_evicts_oldest_when_full():
    c = _LRUCache(maxsize=2, ttl=60)
    c.set("https://a.com/", "DE", b"a")
    c.set("https://b.com/", "DE", b"b")
    c.set("https://c.com/", "DE", b"c")  # should evict "a"
    assert c.get("https://a.com/", "DE") is None
    assert c.get("https://b.com/", "DE") == b"b"
    assert c.get("https://c.com/", "DE") == b"c"


def test_lru_cache_ttl_expiry(monkeypatch):
    import time as _time
    c = _LRUCache(maxsize=10, ttl=1)
    c.set("https://x.com/", "DE", b"data")
    # Fast-forward time by monkeypatching monotonic
    real_monotonic = _time.monotonic
    monkeypatch.setattr(_time, "monotonic", lambda: real_monotonic() + 10)
    import app.main as m
    monkeypatch.setattr(m._time, "monotonic", lambda: real_monotonic() + 10)  # type: ignore
    assert c.get("https://x.com/", "DE") is None


# ── Integration: FastAPI routes ───────────────────────────────────────────────

@pytest_asyncio.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


@pytest.mark.asyncio
async def test_health_endpoint(client):
    resp = await client.get("/worker-health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "deepl_configured" in data


@pytest.mark.asyncio
async def test_proxy_missing_upstream_url_returns_400(client):
    resp = await client.get("/some/path")
    assert resp.status_code == 400
    assert b"X-Upstream-URL" in resp.content


@pytest.mark.asyncio
async def test_proxy_html_response_is_translated(client):
    """Worker fetches HTML from origin → translates → returns translated HTML."""
    import httpx

    mock_origin_response = httpx.Response(
        200,
        content=SIMPLE_HTML,
        headers={"content-type": "text/html; charset=utf-8"},
    )

    with patch("app.main._fetch_origin", new=AsyncMock(return_value=mock_origin_response)), \
         patch("app.main._translate_texts", new=AsyncMock(side_effect=_fake_translate)):

        resp = await client.get(
            "/page",
            headers={"X-Upstream-URL": "https://origin.example.com/page"},
        )

    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Hallo Welt" in resp.text
    assert resp.headers.get("x-translated-to") == "DE"


@pytest.mark.asyncio
async def test_proxy_non_html_passthrough(client):
    """Worker must NOT translate CSS/images/JSON — pass bytes straight through."""
    import httpx

    css_bytes = b"body { font-family: sans-serif; }"
    mock_origin_response = httpx.Response(
        200,
        content=css_bytes,
        headers={"content-type": "text/css"},
    )

    with patch("app.main._fetch_origin", new=AsyncMock(return_value=mock_origin_response)):
        resp = await client.get(
            "/style.css",
            headers={"X-Upstream-URL": "https://origin.example.com/style.css"},
        )

    assert resp.status_code == 200
    assert resp.content == css_bytes
    assert resp.headers.get("x-translated-to") == "none"


@pytest.mark.asyncio
async def test_proxy_origin_timeout_returns_504(client):
    import httpx

    with patch(
        "app.main._fetch_origin",
        new=AsyncMock(side_effect=httpx.TimeoutException("timeout")),
    ):
        resp = await client.get(
            "/page",
            headers={"X-Upstream-URL": "https://origin.example.com/page"},
        )

    assert resp.status_code == 504


@pytest.mark.asyncio
async def test_proxy_html_cached_on_second_get(client):
    """Second identical GET should return X-Translation-Cache: HIT."""
    import httpx

    mock_origin_response = httpx.Response(
        200,
        content=SIMPLE_HTML,
        headers={"content-type": "text/html; charset=utf-8"},
    )

    with patch("app.main._fetch_origin", new=AsyncMock(return_value=mock_origin_response)), \
         patch("app.main._translate_texts", new=AsyncMock(side_effect=_fake_translate)):

        # First request — populates cache
        r1 = await client.get(
            "/cached-page",
            headers={"X-Upstream-URL": "https://origin.example.com/cached-page"},
        )
        assert r1.headers.get("x-translation-cache") == "MISS"

        # Second request — should hit cache (no origin fetch needed)
        r2 = await client.get(
            "/cached-page",
            headers={"X-Upstream-URL": "https://origin.example.com/cached-page"},
        )
        assert r2.headers.get("x-translation-cache") == "HIT"
        assert "Hallo Welt" in r2.text
