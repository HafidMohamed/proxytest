"""
Translation Proxy — Live Browser Demo
======================================
Self-contained demo server that simulates the full system:
  - Control Plane API (customers, domains, glossary, TM, usage)
  - Translation Worker (proxies + translates HTML in real-time)
  - Demo "origin" website to translate
  - Interactive dashboard UI

Run: python3 demo_server.py
Open: http://localhost:8888
"""

import asyncio
import hashlib
import json
import logging
import secrets
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Optional
from unittest.mock import patch, AsyncMock

import httpx
from bs4 import BeautifulSoup, NavigableString, Tag, Comment
from fastapi import FastAPI, Request, Form, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, PlainTextResponse

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("demo")

# ── In-memory "database" ──────────────────────────────────────────────────────

_customers  = {}   # email → {id, raw_key, key_hash, key_prefix, plan, created_at}
_domains    = {}   # domain → {customer_id, backend_url, routing_mode, ...}
_glossary   = {}   # domain → list of {id, source_term, language, replacement}
_tm         = {}   # (hash, lang) → translation
_usage      = {}   # customer_id → {words: int, pages: int}
_pages      = {}   # (domain, lang, url) → html

PLAN_LIMITS = {"free": 2000, "starter": 10000, "pro": 200000}

def _hash(key): return hashlib.sha256(key.encode()).hexdigest()
def _words(text): return len(text.split())

def _get_customer_by_key(raw_key):
    h = _hash(raw_key)
    for c in _customers.values():
        if c["key_hash"] == h:
            return c
    return None

# ── Fake DeepL translator ─────────────────────────────────────────────────────

TRANSLATIONS = {
    "DE": {
        "Welcome": "Willkommen", "Home": "Startseite", "About": "Über uns",
        "Products": "Produkte", "Contact": "Kontakt", "Buy now": "Jetzt kaufen",
        "Add to cart": "In den Warenkorb", "Price": "Preis",
        "Free shipping": "Kostenloser Versand", "Hello": "Hallo",
        "Search": "Suchen", "Login": "Anmelden", "Sign up": "Registrieren",
        "Our amazing products": "Unsere tollen Produkte",
        "Best quality guaranteed": "Beste Qualität garantiert",
        "Shop now and save": "Jetzt einkaufen und sparen",
        "Customer reviews": "Kundenbewertungen",
        "Description": "Beschreibung", "Details": "Details",
        "In stock": "Auf Lager", "Out of stock": "Nicht verfügbar",
        "Sale": "Angebot", "New": "Neu",
    },
    "FR": {
        "Welcome": "Bienvenue", "Home": "Accueil", "About": "À propos",
        "Products": "Produits", "Contact": "Contact", "Buy now": "Acheter maintenant",
        "Add to cart": "Ajouter au panier", "Price": "Prix",
        "Free shipping": "Livraison gratuite", "Hello": "Bonjour",
        "Search": "Rechercher", "Login": "Connexion", "Sign up": "S'inscrire",
        "Our amazing products": "Nos produits incroyables",
        "Best quality guaranteed": "Meilleure qualité garantie",
        "Shop now and save": "Achetez maintenant et économisez",
        "Customer reviews": "Avis clients",
        "Description": "Description", "Details": "Détails",
        "In stock": "En stock", "Out of stock": "Rupture de stock",
        "Sale": "Soldes", "New": "Nouveau",
    },
    "ES": {
        "Welcome": "Bienvenido", "Home": "Inicio", "About": "Sobre nosotros",
        "Products": "Productos", "Contact": "Contacto", "Buy now": "Comprar ahora",
        "Add to cart": "Añadir al carrito", "Price": "Precio",
        "Free shipping": "Envío gratis", "Hello": "Hola",
        "Search": "Buscar", "Login": "Iniciar sesión", "Sign up": "Registrarse",
        "Our amazing products": "Nuestros increíbles productos",
        "Best quality guaranteed": "Mejor calidad garantizada",
        "Shop now and save": "Compra ahora y ahorra",
        "Customer reviews": "Reseñas de clientes",
        "Description": "Descripción", "Details": "Detalles",
        "In stock": "En stock", "Out of stock": "Sin stock",
        "Sale": "Oferta", "New": "Nuevo",
    },
}

def _fake_translate(text: str, lang: str) -> str:
    """Simple word-by-word fake translation for demo."""
    t = TRANSLATIONS.get(lang, {})
    # Check full phrase first
    if text.strip() in t:
        return t[text.strip()]
    # Word by word
    words = text.split()
    result = [t.get(w, w) for w in words]
    return " ".join(result)

# ── Origin demo website ────────────────────────────────────────────────────────

ORIGIN_HTML = {
    "/": """<!DOCTYPE html><html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Welcome - DemoShop</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: 'Segoe UI', sans-serif; background: #f8f9fa; color: #333; }}
    nav {{ background: #1a1a2e; color: white; padding: 1rem 2rem; display: flex; justify-content: space-between; align-items: center; }}
    nav a {{ color: white; text-decoration: none; margin-left: 1.5rem; }}
    .hero {{ background: linear-gradient(135deg, #667eea, #764ba2); color: white; padding: 5rem 2rem; text-align: center; }}
    .hero h1 {{ font-size: 3rem; margin-bottom: 1rem; }}
    .hero p {{ font-size: 1.3rem; margin-bottom: 2rem; opacity: 0.9; }}
    .btn {{ background: white; color: #667eea; padding: 0.8rem 2rem; border-radius: 50px; font-weight: bold; text-decoration: none; display: inline-block; }}
    .products {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 2rem; padding: 3rem 2rem; max-width: 1100px; margin: 0 auto; }}
    .card {{ background: white; border-radius: 12px; overflow: hidden; box-shadow: 0 2px 10px rgba(0,0,0,.08); }}
    .card-img {{ height: 200px; display: flex; align-items: center; justify-content: center; font-size: 4rem; }}
    .card-body {{ padding: 1.2rem; }}
    .card-body h3 {{ margin-bottom: 0.5rem; }}
    .badge {{ background: #e8f5e9; color: #2e7d32; padding: 0.2rem 0.6rem; border-radius: 4px; font-size: 0.8rem; }}
    .price {{ font-size: 1.4rem; font-weight: bold; color: #667eea; margin: 0.8rem 0; }}
    .btn-card {{ width: 100%; padding: 0.7rem; border: none; background: #667eea; color: white; border-radius: 8px; cursor: pointer; font-size: 1rem; }}
    footer {{ background: #1a1a2e; color: #aaa; text-align: center; padding: 2rem; margin-top: 2rem; }}
  </style>
</head>
<body>
  <nav>
    <strong>🛍️ DemoShop</strong>
    <div>
      <a href="/">Home</a>
      <a href="/about">About</a>
      <a href="/products">Products</a>
      <a href="/contact">Contact</a>
    </div>
  </nav>
  <div class="hero">
    <h1>Welcome to DemoShop</h1>
    <p>Our amazing products — Best quality guaranteed</p>
    <a href="/products" class="btn">Shop now and save</a>
  </div>
  <div class="products">
    <div class="card">
      <div class="card-img" style="background:#e3f2fd">📱</div>
      <div class="card-body">
        <span class="badge">New</span>
        <h3>Smart Phone Pro</h3>
        <p>Best quality guaranteed</p>
        <div class="price">€299</div>
        <span class="badge">Free shipping</span>
        <button class="btn-card">Add to cart</button>
      </div>
    </div>
    <div class="card">
      <div class="card-img" style="background:#f3e5f5">💻</div>
      <div class="card-body">
        <span class="badge">Sale</span>
        <h3>Laptop Ultra</h3>
        <p>Best quality guaranteed</p>
        <div class="price">€899</div>
        <span class="badge">In stock</span>
        <button class="btn-card">Add to cart</button>
      </div>
    </div>
    <div class="card">
      <div class="card-img" style="background:#e8f5e9">🎧</div>
      <div class="card-body">
        <span class="badge">New</span>
        <h3>Wireless Headphones</h3>
        <p>Customer reviews: ⭐⭐⭐⭐⭐</p>
        <div class="price">€149</div>
        <span class="badge">Free shipping</span>
        <button class="btn-card">Add to cart</button>
      </div>
    </div>
  </div>
  <footer>
    <p>© 2026 DemoShop — Description | Details | Contact</p>
  </footer>
</body>
</html>""",

    "/about": """<!DOCTYPE html><html lang="en">
<head><meta charset="UTF-8"><title>About - DemoShop</title>
<style>body{{font-family:'Segoe UI',sans-serif;max-width:800px;margin:0 auto;padding:2rem;}}
nav{{background:#1a1a2e;color:white;padding:1rem 2rem;display:flex;justify-content:space-between;margin:-2rem -2rem 2rem;}}
nav a{{color:white;text-decoration:none;margin-left:1.5rem;}}</style></head>
<body>
  <nav><strong>🛍️ DemoShop</strong>
  <div><a href="/">Home</a><a href="/about">About</a><a href="/products">Products</a></div></nav>
  <h1>About us</h1>
  <p>Welcome to DemoShop. Best quality guaranteed since 2020.</p>
  <p>Free shipping on all orders over €50. Customer reviews speak for themselves.</p>
  <p>Contact us: hello@demoshop.example</p>
</body></html>""",

    "/products": """<!DOCTYPE html><html lang="en">
<head><meta charset="UTF-8"><title>Products - DemoShop</title>
<style>body{{font-family:'Segoe UI',sans-serif;padding:2rem;}}
nav{{background:#1a1a2e;color:white;padding:1rem 2rem;display:flex;justify-content:space-between;margin:-2rem -2rem 2rem;}}
nav a{{color:white;text-decoration:none;margin-left:1.5rem;}}
.grid{{display:grid;grid-template-columns:repeat(2,1fr);gap:1rem;margin-top:1rem;}}
.item{{border:1px solid #eee;border-radius:8px;padding:1rem;}}</style></head>
<body>
  <nav><strong>🛍️ DemoShop</strong>
  <div><a href="/">Home</a><a href="/about">About</a><a href="/products">Products</a></div></nav>
  <h1>Products</h1>
  <p>Our amazing products — all with free shipping and best quality guaranteed.</p>
  <div class="grid">
    <div class="item"><h3>📱 Smart Phone Pro</h3><p>In stock</p><strong>€299</strong></div>
    <div class="item"><h3>💻 Laptop Ultra</h3><p>In stock</p><strong>€899</strong></div>
    <div class="item"><h3>🎧 Wireless Headphones</h3><p>In stock</p><strong>€149</strong></div>
    <div class="item"><h3>⌚ Smart Watch</h3><p>Out of stock</p><strong>€199</strong></div>
  </div>
</body></html>""",
}

# ── HTML translation (demo version, no real DeepL) ────────────────────────────

SKIP_TAGS = frozenset({"script","style","code","pre","textarea","noscript","template","svg","math"})
TRANSLATE_ATTRS = frozenset({"alt","title","placeholder","aria-label"})

def _translate_html(html: str, lang: str, domain: str,
                    all_languages: list, routing_mode: str = "subdirectory") -> str:
    soup = BeautifulSoup(html, "html.parser")
    word_count = 0

    # Apply glossary
    glossary = _glossary.get(domain, [])

    # Walk text nodes
    def _walk(tag):
        for child in list(tag.children):
            if isinstance(child, Comment):
                continue
            if isinstance(child, NavigableString):
                text = str(child).strip()
                if not text:
                    continue
                # TM check
                tm_key = (_hash(text), lang)
                if tm_key in _tm:
                    child.replace_with(NavigableString(_tm[tm_key]))
                    continue
                # Apply glossary protection
                translated = _fake_translate(str(child), lang)
                # Apply glossary replacements
                for rule in glossary:
                    if rule.get("language") is None or rule.get("language") == lang:
                        term = rule["source_term"]
                        repl = rule.get("replacement") or term  # None = keep original
                        translated = translated.replace(term, repl)
                _tm[tm_key] = translated
                child.replace_with(NavigableString(translated))
            elif isinstance(child, Tag) and child.name not in SKIP_TAGS:
                _walk(child)

    _walk(soup)

    # Translate attributes
    for tag in soup.find_all(True):
        for attr in TRANSLATE_ATTRS:
            val = tag.get(attr, "")
            if val.strip():
                tag[attr] = _fake_translate(val, lang)

    # Inject hreflang
    head = soup.find("head")
    if head:
        for old in head.find_all("link", rel="alternate"):
            if old.get("hreflang"): old.decompose()
        orig_path = "/"
        for atag in head.find_all("link", hreflang=True): pass
        def make_link(hl, href):
            t = soup.new_tag("link", rel="alternate", hreflang=hl, href=href)
            head.append(t)
        make_link("en", f"http://localhost:8888/{domain}/")
        for l in all_languages:
            make_link(l.lower(), f"http://localhost:8888/{domain}/{l.lower()}/")
        make_link("x-default", f"http://localhost:8888/{domain}/")

    # Inject language switcher
    body = soup.find("body")
    if body:
        lang_names = {"DE":"🇩🇪 Deutsch","FR":"🇫🇷 Français","ES":"🇪🇸 Español"}
        opts = '<option value="__orig__">🌐 English (original)</option>'
        for l in sorted(all_languages):
            sel = " selected" if l.upper() == lang.upper() else ""
            opts += f'<option value="{l}"{sel}>{lang_names.get(l, l)}</option>'
        switcher = f"""<div id="__proxy_lang_switcher__" style="
            position:fixed;bottom:20px;right:20px;z-index:99999;
            background:white;border:2px solid #667eea;border-radius:12px;
            box-shadow:0 4px 20px rgba(102,126,234,.3);padding:10px 16px;
            font-family:system-ui,sans-serif;display:flex;align-items:center;gap:10px;">
          <span style="font-size:20px;">🌐</span>
          <select style="border:none;outline:none;background:transparent;font-size:14px;cursor:pointer;"
                  onchange="(function(v){{
                    if(v==='__orig__') window.location='http://localhost:8888/{domain}/';
                    else window.location='http://localhost:8888/{domain}/'+v+'/';
                  }})(this.value)">{opts}</select>
        </div>"""
        body.append(BeautifulSoup(switcher, "html.parser"))

    # Add demo banner at top
    banner = soup.new_tag("div")
    banner["style"] = ("background:#1a1a2e;color:#a78bfa;text-align:center;padding:8px;font-size:13px;"
                       "font-family:monospace;letter-spacing:.5px;")
    banner.string = f"🔀 Translation Proxy Demo — Language: {lang} | Routing: {routing_mode} | " \
                    f"hreflang ✓ | Switcher ✓ | TM ✓"
    body.insert(0, banner)

    return str(soup)

# ── Dashboard HTML ────────────────────────────────────────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Translation Proxy — Live Demo</title>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:'Segoe UI',system-ui,sans-serif;background:#0f0f1a;color:#e2e8f0;min-height:100vh}
    .header{background:linear-gradient(135deg,#1a1a2e,#16213e);padding:2rem;border-bottom:1px solid #2d3748}
    .header h1{font-size:1.8rem;color:#a78bfa;font-weight:700}
    .header p{color:#94a3b8;margin-top:.4rem}
    .badge{background:#7c3aed22;border:1px solid #7c3aed;color:#a78bfa;padding:.2rem .7rem;border-radius:20px;font-size:.75rem;margin-left:.5rem}
    .container{max-width:1200px;margin:0 auto;padding:2rem}
    .grid{display:grid;grid-template-columns:1fr 1fr;gap:1.5rem;margin-bottom:2rem}
    .card{background:#1a1a2e;border:1px solid #2d3748;border-radius:12px;padding:1.5rem}
    .card h2{font-size:1rem;color:#94a3b8;text-transform:uppercase;letter-spacing:.1em;margin-bottom:1rem}
    .section-title{font-size:1.3rem;font-weight:600;color:#e2e8f0;margin-bottom:1.5rem;margin-top:1rem;display:flex;align-items:center;gap:.5rem}
    label{display:block;font-size:.85rem;color:#94a3b8;margin-bottom:.4rem;margin-top:.8rem}
    input,select{width:100%;padding:.6rem .9rem;background:#0f172a;border:1px solid #374151;border-radius:8px;color:#e2e8f0;font-size:.9rem}
    input:focus,select:focus{outline:none;border-color:#7c3aed}
    .btn{padding:.65rem 1.4rem;border:none;border-radius:8px;cursor:pointer;font-size:.9rem;font-weight:600;transition:.2s}
    .btn-primary{background:#7c3aed;color:white}
    .btn-primary:hover{background:#6d28d9}
    .btn-sm{padding:.4rem .9rem;font-size:.8rem}
    .btn-success{background:#065f46;color:#6ee7b7}
    .btn-danger{background:#7f1d1d;color:#fca5a5}
    .output{background:#0f172a;border:1px solid #1e3a5f;border-radius:8px;padding:1rem;margin-top:.8rem;font-size:.85rem;min-height:60px;white-space:pre-wrap;word-break:break-all;color:#7dd3fc;font-family:monospace}
    .key-box{background:#0f172a;border:1px solid #f59e0b55;border-radius:8px;padding:.8rem 1rem;color:#fbbf24;font-family:monospace;font-size:.85rem;margin-top:.8rem;word-break:break-all}
    .stat{text-align:center;padding:1rem}
    .stat-value{font-size:2rem;font-weight:700;color:#a78bfa}
    .stat-label{font-size:.8rem;color:#64748b;margin-top:.3rem}
    .progress{height:8px;background:#1e293b;border-radius:4px;margin:.5rem 0}
    .progress-bar{height:100%;border-radius:4px;background:linear-gradient(90deg,#7c3aed,#a78bfa);transition:.5s}
    .preview-links a{display:inline-block;margin:.4rem .4rem .4rem 0;padding:.5rem 1rem;background:#1e293b;border:1px solid #374151;border-radius:8px;color:#a78bfa;text-decoration:none;font-size:.85rem}
    .preview-links a:hover{border-color:#7c3aed;background:#1a1a2e}
    .tag{display:inline-block;padding:.15rem .5rem;border-radius:4px;font-size:.75rem;margin:.1rem;background:#1e3a5f;color:#7dd3fc}
    .tag.green{background:#064e3b;color:#6ee7b7}
    .tag.purple{background:#2d1b69;color:#c4b5fd}
    table{width:100%;border-collapse:collapse;font-size:.85rem}
    th{text-align:left;padding:.6rem;color:#64748b;font-weight:500;border-bottom:1px solid #1e293b}
    td{padding:.6rem;border-bottom:1px solid #1a1a2e;color:#cbd5e1}
    .alert{padding:.8rem 1rem;border-radius:8px;margin-top:.8rem;font-size:.85rem}
    .alert-success{background:#064e3b22;border:1px solid #065f46;color:#6ee7b7}
    .alert-error{background:#7f1d1d22;border:1px solid #7f1d1d;color:#fca5a5}
    .tabs{display:flex;gap:.5rem;margin-bottom:1.5rem;border-bottom:1px solid #1e293b;padding-bottom:.5rem}
    .tab{padding:.5rem 1.2rem;border-radius:8px 8px 0 0;cursor:pointer;font-size:.9rem;color:#64748b}
    .tab.active{background:#7c3aed22;color:#a78bfa;border:1px solid #7c3aed;border-bottom:none}
    #status-bar{position:fixed;bottom:0;left:0;right:0;background:#1a1a2e;border-top:1px solid #2d3748;padding:.5rem 2rem;font-size:.8rem;color:#64748b;font-family:monospace}
  </style>
</head>
<body>
<div class="header">
  <h1>🔀 Translation Proxy <span class="badge">Live Demo</span></h1>
  <p>Full system simulation: Control Plane API + Translation Worker + Nginx routing</p>
</div>

<div class="container">

  <!-- TABS -->
  <div class="tabs">
    <div class="tab active" onclick="showTab('setup')">⚙️ Setup</div>
    <div class="tab" onclick="showTab('translate')">🌍 Translate</div>
    <div class="tab" onclick="showTab('glossary')">📖 Glossary</div>
    <div class="tab" onclick="showTab('memory')">🧠 TM & Usage</div>
    <div class="tab" onclick="showTab('api')">🔌 API Tester</div>
  </div>

  <!-- SETUP TAB -->
  <div id="tab-setup">
    <div class="grid">
      <div class="card">
        <h2>Step 1 — Create Account</h2>
        <label>Your email</label>
        <input id="reg-email" type="email" placeholder="you@example.com" value="demo@myshop.com">
        <button class="btn btn-primary" style="margin-top:.8rem;width:100%" onclick="register()">Create Customer Account</button>
        <div id="reg-result"></div>
        <div id="key-display" style="display:none">
          <div class="key-box" id="key-value"></div>
          <p style="font-size:.75rem;color:#f59e0b;margin-top:.5rem">⚠️ Save this key — shown only once!</p>
        </div>
      </div>
      <div class="card">
        <h2>Step 2 — Register Domain</h2>
        <label>Your API key (from Step 1)</label>
        <input id="domain-key" type="text" placeholder="sk-...">
        <label>Domain to translate</label>
        <input id="domain-name" value="myshop.demo" placeholder="shop.example.com">
        <label>Origin URL (demo server)</label>
        <input id="backend-url" value="http://localhost:8888/origin" readonly>
        <label>Routing mode</label>
        <select id="routing-mode">
          <option value="subdirectory">Subdirectory (/de/, /fr/)</option>
          <option value="subdomain">Subdomain (de.domain.com)</option>
        </select>
        <button class="btn btn-primary" style="margin-top:.8rem;width:100%" onclick="registerDomain()">Register Domain</button>
        <div id="domain-result"></div>
      </div>
    </div>
    <div class="card">
      <h2>Step 3 — Configure Languages</h2>
      <div class="grid" style="gap:1rem">
        <div>
          <label>API key</label>
          <input id="cfg-key" type="text" placeholder="sk-...">
          <label>Languages (comma-separated DeepL codes)</label>
          <input id="cfg-langs" value="DE,FR,ES" placeholder="DE,FR,ES">
          <label>Crawl frequency</label>
          <select id="cfg-freq">
            <option value="daily">Daily</option>
            <option value="hourly">Hourly</option>
            <option value="weekly">Weekly</option>
            <option value="manual" selected>Manual</option>
          </select>
        </div>
        <div>
          <label>Domain</label>
          <input id="cfg-domain" value="myshop.demo">
          <label>Extra URLs to always crawl (one per line)</label>
          <textarea id="cfg-extra" style="width:100%;height:80px;background:#0f172a;border:1px solid #374151;color:#e2e8f0;border-radius:8px;padding:.6rem;font-size:.85rem" placeholder="/special-page&#10;/landing"></textarea>
        </div>
      </div>
      <button class="btn btn-primary" style="margin-top:1rem" onclick="saveConfig()">Save Config & Trigger Crawl</button>
      <div id="cfg-result"></div>
    </div>
  </div>

  <!-- TRANSLATE TAB -->
  <div id="tab-translate" style="display:none">
    <div class="section-title">🌍 Live Translation Preview</div>
    <div class="card" style="margin-bottom:1.5rem">
      <h2>Preview translated pages</h2>
      <p style="color:#94a3b8;font-size:.85rem;margin-bottom:1rem">
        These links open live translated pages using the demo origin. The language switcher, hreflang tags, and translation banner are all injected automatically.
      </p>
      <div class="preview-links" id="preview-links">
        <p style="color:#64748b">Register a domain first (Setup tab)</p>
      </div>
      <div style="margin-top:1.5rem">
        <h2>Translation Stats</h2>
        <div id="tm-live-stats" style="color:#64748b;font-size:.85rem">—</div>
      </div>
    </div>
    <div class="card">
      <h2>Manual crawl trigger</h2>
      <label>API key</label><input id="crawl-key" placeholder="sk-...">
      <label>Domain</label><input id="crawl-domain" value="myshop.demo">
      <button class="btn btn-primary" style="margin-top:.8rem" onclick="triggerCrawl()">🚀 Run Crawl Now</button>
      <div id="crawl-result"></div>
    </div>
  </div>

  <!-- GLOSSARY TAB -->
  <div id="tab-glossary" style="display:none">
    <div class="section-title">📖 Glossary Rules</div>
    <div class="grid">
      <div class="card">
        <h2>Add rule</h2>
        <label>API key</label><input id="gl-key" placeholder="sk-...">
        <label>Domain</label><input id="gl-domain" value="myshop.demo">
        <label>Source term (your brand / technical term)</label>
        <input id="gl-term" placeholder="DemoShop" value="DemoShop">
        <label>Replacement (leave blank = never translate)</label>
        <input id="gl-replacement" placeholder="Leave blank to keep original">
        <label>Language (blank = all languages)</label>
        <input id="gl-lang" placeholder="DE (blank = all)">
        <button class="btn btn-primary" style="margin-top:.8rem;width:100%" onclick="addGlossary()">Add Rule</button>
        <div id="gl-result"></div>
      </div>
      <div class="card">
        <h2>Active rules</h2>
        <button class="btn btn-sm" style="margin-bottom:1rem" onclick="loadGlossary()">🔄 Refresh</button>
        <table>
          <thead><tr><th>Term</th><th>Replacement</th><th>Lang</th><th></th></tr></thead>
          <tbody id="gl-table"><tr><td colspan="4" style="color:#64748b">Load rules above</td></tr></tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- TM & USAGE TAB -->
  <div id="tab-memory" style="display:none">
    <div class="section-title">🧠 Translation Memory & Usage</div>
    <div class="grid">
      <div class="card">
        <h2>Translation Memory</h2>
        <button class="btn btn-sm btn-success" onclick="loadTMStats()">🔄 Load Stats</button>
        <div id="tm-stats" style="margin-top:1rem;color:#64748b">Click to load</div>
      </div>
      <div class="card">
        <h2>Usage & Billing</h2>
        <label>API key</label><input id="usage-key" placeholder="sk-...">
        <button class="btn btn-sm btn-success" style="margin-top:.5rem" onclick="loadUsage()">🔄 Load Usage</button>
        <div id="usage-display" style="margin-top:1rem;color:#64748b">Enter key and load</div>
      </div>
    </div>
    <div class="card" style="margin-top:1.5rem">
      <h2>How Translation Memory saves cost</h2>
      <p style="color:#94a3b8;font-size:.85rem;line-height:1.7;margin-top:.5rem">
        ✅ <strong>Without TM:</strong> "Add to cart" on 50 pages = 50 DeepL API calls<br>
        ✅ <strong>With TM:</strong> "Add to cart" translated once, 49 instant cache hits<br>
        ✅ <strong>Hot cache:</strong> In-process LRU (50k entries) — sub-millisecond<br>
        ✅ <strong>DB cache:</strong> PostgreSQL table shared across all workers — survives restarts<br>
        💰 <strong>Typical savings: 60–80% fewer DeepL API calls</strong>
      </p>
    </div>
  </div>

  <!-- API TESTER TAB -->
  <div id="tab-api" style="display:none">
    <div class="section-title">🔌 Live API Tester</div>
    <div class="card">
      <label>Method</label>
      <select id="api-method">
        <option>GET</option><option>POST</option><option>DELETE</option>
      </select>
      <label>Endpoint</label>
      <select id="api-endpoint" onchange="updateApiBody()">
        <option value="/health">GET /health</option>
        <option value="/customers">POST /customers</option>
        <option value="/customers/me">GET /customers/me</option>
        <option value="/domains">POST /domains</option>
        <option value="/domains">GET /domains</option>
        <option value="/usage">GET /usage</option>
        <option value="/system/translation-memory/stats">GET /system/translation-memory/stats</option>
      </select>
      <label>API key (for authenticated endpoints)</label>
      <input id="api-key" placeholder="sk-...">
      <label>Request body (JSON, for POST)</label>
      <textarea id="api-body" style="width:100%;height:100px;background:#0f172a;border:1px solid #374151;color:#e2e8f0;border-radius:8px;padding:.6rem;font-family:monospace;font-size:.85rem">{}</textarea>
      <button class="btn btn-primary" style="margin-top:.8rem" onclick="callApi()">Send Request</button>
      <div id="api-response" class="output" style="margin-top:.8rem;max-height:300px;overflow:auto">Response will appear here</div>
    </div>
  </div>

</div>

<div id="status-bar">Ready — Demo Translation Proxy v2.0 running on localhost:8888</div>

<script>
const BASE = '';
let _apiKey = '';

function showTab(name) {
  document.querySelectorAll('[id^="tab-"]').forEach(el => el.style.display = 'none');
  document.getElementById('tab-' + name).style.display = 'block';
  document.querySelectorAll('.tab').forEach((t,i) => {
    const names = ['setup','translate','glossary','memory','api'];
    t.classList.toggle('active', names[i] === name);
  });
  if (name === 'translate') refreshPreviewLinks();
  if (name === 'memory') loadTMStats();
}

function status(msg) {
  document.getElementById('status-bar').textContent = msg;
}

function syncKeys(key) {
  if (!key) return;
  _apiKey = key;
  ['domain-key','cfg-key','crawl-key','gl-key','usage-key','api-key'].forEach(id => {
    const el = document.getElementById(id);
    if (el && !el.value) el.value = key;
  });
}

async function api(method, path, body=null, key=null) {
  const headers = {'Content-Type':'application/json'};
  if (key || _apiKey) headers['X-API-Key'] = key || _apiKey;
  const opts = {method, headers};
  if (body) opts.body = JSON.stringify(body);
  status('⏳ ' + method + ' ' + path + '...');
  try {
    const r = await fetch(BASE + '/api' + path, opts);
    const data = await r.json().catch(() => ({}));
    status('✅ ' + method + ' ' + path + ' → ' + r.status);
    return {status: r.status, data};
  } catch(e) {
    status('❌ Error: ' + e.message);
    return {status: 0, data: {error: e.message}};
  }
}

function result(elId, {status, data}, successStatus=200) {
  const el = document.getElementById(elId);
  const ok = status === successStatus || status === 201 || status === 204;
  el.className = 'alert ' + (ok ? 'alert-success' : 'alert-error');
  el.textContent = JSON.stringify(data, null, 2);
}

async function register() {
  const email = document.getElementById('reg-email').value;
  const r = await api('POST', '/customers', {email});
  result('reg-result', r, 201);
  if (r.status === 201) {
    document.getElementById('key-display').style.display = 'block';
    document.getElementById('key-value').textContent = r.data.raw_api_key;
    syncKeys(r.data.raw_api_key);
  }
}

async function registerDomain() {
  const key    = document.getElementById('domain-key').value;
  const domain = document.getElementById('domain-name').value;
  const burl   = document.getElementById('backend-url').value;
  const mode   = document.getElementById('routing-mode').value;
  syncKeys(key);
  const r = await api('POST', '/domains', {domain, backend_url: burl, routing_mode: mode}, key);
  result('domain-result', r, 201);
  refreshPreviewLinks();
}

async function saveConfig() {
  const key    = document.getElementById('cfg-key').value;
  const domain = document.getElementById('cfg-domain').value;
  const langs  = document.getElementById('cfg-langs').value;
  const freq   = document.getElementById('cfg-freq').value;
  const extra  = document.getElementById('cfg-extra').value;
  syncKeys(key);
  const r = await api('POST', '/translation/' + domain + '/config',
    {languages: langs, frequency: freq, extra_urls: extra || null, routing_mode: 'subdirectory'}, key);
  result('cfg-result', r);
  refreshPreviewLinks();
}

function refreshPreviewLinks() {
  const domain = document.getElementById('cfg-domain').value || document.getElementById('domain-name').value || 'myshop.demo';
  const langs = (document.getElementById('cfg-langs').value || 'DE,FR,ES').split(',').map(l=>l.trim());
  const el = document.getElementById('preview-links');
  let html = '<strong style="font-size:.85rem;color:#94a3b8;display:block;margin-bottom:.5rem">Origin (English):</strong>';
  html += `<a href="/origin/" target="_blank">🌐 english (original)</a>`;
  html += `<a href="/origin/about" target="_blank">🌐 /about</a>`;
  html += `<a href="/origin/products" target="_blank">🌐 /products</a>`;
  html += '<br><strong style="font-size:.85rem;color:#94a3b8;display:block;margin:.8rem 0 .5rem">Translated (live proxy):</strong>';
  langs.forEach(lang => {
    const l = lang.toLowerCase();
    const names = {de:'🇩🇪 Deutsch', fr:'🇫🇷 Français', es:'🇪🇸 Español'};
    html += `<a href="/proxy/${domain}/${l}/" target="_blank">${names[l]||lang} Home</a>`;
    html += `<a href="/proxy/${domain}/${l}/about" target="_blank">${names[l]||lang} About</a>`;
    html += `<a href="/proxy/${domain}/${l}/products" target="_blank">${names[l]||lang} Products</a>`;
    html += '<br>';
  });
  el.innerHTML = html;
  loadTMStats();
}

async function triggerCrawl() {
  const key    = document.getElementById('crawl-key').value;
  const domain = document.getElementById('crawl-domain').value;
  syncKeys(key);
  document.getElementById('crawl-result').className = 'alert';
  document.getElementById('crawl-result').textContent = '⏳ Crawling... this may take a moment';
  const r = await api('POST', '/translation/' + domain + '/crawl-now', null, key);
  result('crawl-result', r);
  loadTMStats();
}

async function addGlossary() {
  const key    = document.getElementById('gl-key').value;
  const domain = document.getElementById('gl-domain').value;
  const term   = document.getElementById('gl-term').value;
  const repl   = document.getElementById('gl-replacement').value || null;
  const lang   = document.getElementById('gl-lang').value.toUpperCase() || null;
  syncKeys(key);
  const r = await api('POST', '/translation/' + domain + '/glossary',
    {source_term: term, replacement: repl, language: lang, case_sensitive: false}, key);
  result('gl-result', r, 201);
  loadGlossary();
}

async function loadGlossary() {
  const key    = document.getElementById('gl-key').value;
  const domain = document.getElementById('gl-domain').value;
  const r = await api('GET', '/translation/' + domain + '/glossary', null, key);
  const tbody = document.getElementById('gl-table');
  if (!Array.isArray(r.data)) { tbody.innerHTML = '<tr><td colspan="4" style="color:#f87171">'+JSON.stringify(r.data)+'</td></tr>'; return; }
  tbody.innerHTML = r.data.map(rule => `
    <tr>
      <td>${rule.source_term}</td>
      <td>${rule.replacement || '<em style="color:#64748b">keep original</em>'}</td>
      <td>${rule.language || '<em style="color:#64748b">all</em>'}</td>
      <td><button class="btn btn-sm btn-danger" onclick="deleteRule('${rule.id}')">✕</button></td>
    </tr>`).join('') || '<tr><td colspan="4" style="color:#64748b">No rules yet</td></tr>';
}

async function deleteRule(id) {
  const key    = document.getElementById('gl-key').value;
  const domain = document.getElementById('gl-domain').value;
  await api('DELETE', '/translation/' + domain + '/glossary/' + id, null, key);
  loadGlossary();
}

async function loadTMStats() {
  const r = await api('GET', '/system/translation-memory/stats', null, _apiKey);
  if (r.status === 200) {
    const d = r.data;
    document.getElementById('tm-stats').innerHTML = `
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:1rem;margin-top:.5rem">
        <div class="stat"><div class="stat-value">${d.total_entries}</div><div class="stat-label">Cached Translations</div></div>
        <div class="stat"><div class="stat-value">${d.total_cache_hits}</div><div class="stat-label">Cache Hits (saved API calls)</div></div>
        <div class="stat"><div class="stat-value">${d.hot_cache_size}</div><div class="stat-label">In-Memory (hot cache)</div></div>
      </div>
      <div style="margin-top:1rem;font-size:.85rem">By language: ${JSON.stringify(d.by_language)}</div>
    `;
    document.getElementById('tm-live-stats').innerHTML =
      `TM: <span style="color:#a78bfa">${d.total_entries}</span> entries, `+
      `<span style="color:#6ee7b7">${d.total_cache_hits}</span> cache hits saved`;
  }
}

async function loadUsage() {
  const key = document.getElementById('usage-key').value;
  syncKeys(key);
  const r = await api('GET', '/usage', null, key);
  if (r.status === 200) {
    const d = r.data;
    const pct = d.word_percent_used;
    document.getElementById('usage-display').innerHTML = `
      <div style="margin-top:.5rem">
        <strong style="color:#e2e8f0">${d.words_translated.toLocaleString()}</strong>
        <span style="color:#64748b"> / ${d.word_limit.toLocaleString()} words</span>
        <span class="tag ${d.over_limit ? '' : 'green'}">${d.plan.toUpperCase()}</span>
      </div>
      <div class="progress"><div class="progress-bar" style="width:${Math.min(pct,100)}%"></div></div>
      <div style="font-size:.8rem;color:#64748b">${pct}% used — ${d.month}/${d.year}</div>
      <div style="font-size:.8rem;color:#64748b;margin-top:.5rem">Pages served: ${d.pages_served} | Crawls: ${d.crawls_run}</div>
      ${d.over_limit ? '<div class="alert alert-error" style="margin-top:.5rem">⚠️ Monthly limit reached — upgrade plan</div>' : ''}
    `;
  } else {
    document.getElementById('usage-display').textContent = JSON.stringify(r.data);
  }
}

const API_BODIES = {
  '/customers': '{"email": "you@example.com"}',
  '/domains': '{"domain": "myshop.demo", "backend_url": "http://localhost:8888/origin", "routing_mode": "subdirectory"}',
};
function updateApiBody() {
  const ep = document.getElementById('api-endpoint').value;
  document.getElementById('api-body').value = API_BODIES[ep] || '{}';
  const method = ep.startsWith('/customers') && document.getElementById('api-method').value === 'GET' ? 'GET' : 'POST';
}

async function callApi() {
  const method = document.getElementById('api-method').value;
  const path   = document.getElementById('api-endpoint').value;
  const key    = document.getElementById('api-key').value;
  const body   = document.getElementById('api-body').value;
  syncKeys(key);
  let parsed = null;
  if (body.trim() !== '{}' && body.trim()) {
    try { parsed = JSON.parse(body); } catch(e) {}
  }
  const r = await api(method, path, parsed, key);
  document.getElementById('api-response').textContent = JSON.stringify(r, null, 2);
}
</script>
</body>
</html>"""

# ── FastAPI demo app ──────────────────────────────────────────────────────────

app = FastAPI(title="Translation Proxy Demo", docs_url=None, redoc_url=None)

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return DASHBOARD_HTML

# Origin pages (the "customer's website" we're translating)
@app.get("/origin/{path:path}", response_class=HTMLResponse)
async def origin_page(path: str):
    clean = "/" + path.lstrip("/") if path else "/"
    clean = clean.rstrip("/") or "/"
    html = ORIGIN_HTML.get(clean) or ORIGIN_HTML.get("/" + path.split("/")[0]) or ORIGIN_HTML["/"]
    return html

@app.get("/origin", response_class=HTMLResponse)
async def origin_root():
    return ORIGIN_HTML["/"]

# Proxy + translate endpoint
@app.get("/proxy/{domain}/{lang}/{path:path}", response_class=HTMLResponse)
async def proxy_translate(domain: str, lang: str, path: str, request: Request):
    clean = "/" + path.lstrip("/") if path else "/"
    clean = clean.rstrip("/") or "/"
    
    # Get domain config from memory
    dom = _domains.get(domain, {})
    cfg = dom.get("config", {})
    all_langs = [l.strip().upper() for l in cfg.get("languages", lang).split(",") if l.strip()]
    routing   = dom.get("routing_mode", "subdirectory")
    
    # Get origin HTML (simulate fetching from backend)
    origin_html = ORIGIN_HTML.get(clean) or ORIGIN_HTML.get("/" + path.split("/")[0]) or ORIGIN_HTML["/"]
    
    # Check page cache
    cache_key = (domain, lang.upper(), clean)
    if cache_key in _pages:
        return _pages[cache_key]
    
    # Translate
    translated = _translate_html(origin_html, lang.upper(), domain, all_langs, routing)
    _pages[cache_key] = translated
    return translated

@app.get("/proxy/{domain}/{lang}/", response_class=HTMLResponse)
async def proxy_translate_root(domain: str, lang: str, request: Request):
    return await proxy_translate(domain, lang, "", request)

# API passthrough (wraps in-memory store)
@app.post("/api/customers")
async def api_create_customer(request: Request):
    body = await request.json()
    email = body.get("email", "")
    if not email:
        return JSONResponse({"detail": "Email required"}, 422)
    if email in _customers:
        return JSONResponse({"detail": "Email already registered"}, 409)
    raw_key, key_hash, key_prefix = (
        "sk-" + secrets.token_urlsafe(24),
        None, None
    )
    key_hash   = _hash(raw_key)
    key_prefix = raw_key[:12]
    cid = str(uuid.uuid4())
    _customers[email] = {
        "id": cid, "email": email, "raw_key": raw_key,
        "key_hash": key_hash, "key_prefix": key_prefix,
        "plan": "free", "created_at": datetime.now(timezone.utc).isoformat()
    }
    _usage[cid] = {"words": 0, "pages": 0}
    return JSONResponse({
        "id": cid, "email": email, "raw_api_key": raw_key,
        "api_key_prefix": key_prefix, "is_active": True,
        "plan": "free", "created_at": _customers[email]["created_at"]
    }, 201)

@app.get("/api/customers/me")
async def api_me(x_api_key: str = Header(None, alias="X-API-Key")):
    c = _get_customer_by_key(x_api_key or "")
    if not c:
        raise HTTPException(401, "Invalid API key")
    return {k: v for k, v in c.items() if k not in ("raw_key", "key_hash")}

@app.post("/api/domains")
async def api_register_domain(request: Request, x_api_key: str = Header(None, alias="X-API-Key")):
    c = _get_customer_by_key(x_api_key or "")
    if not c: raise HTTPException(401, "Invalid API key")
    body = await request.json()
    domain = body.get("domain", "")
    burl   = body.get("backend_url", "")
    mode   = body.get("routing_mode", "subdirectory")
    if not domain or not burl:
        raise HTTPException(422, "domain and backend_url required")
    token = secrets.token_urlsafe(16)
    _domains[domain] = {
        "id": str(uuid.uuid4()), "customer_id": c["id"],
        "domain": domain, "backend_url": burl,
        "routing_mode": mode, "verification_token": token,
        "is_verified": True, "status": "active", "ssl_status": "active",
        "config": {}
    }
    return JSONResponse({
        "domain": domain,
        "txt_record_name": f"_proxy-verify.{domain}",
        "txt_record_value": f"proxy-verify={token}",
        "a_record_value": "YOUR_SERVER_IP",
        "instructions": f"Add TXT _proxy-verify.{domain} = proxy-verify={token}\nAdd A {domain} = YOUR_SERVER_IP"
    }, 201)

@app.get("/api/domains")
async def api_list_domains(x_api_key: str = Header(None, alias="X-API-Key")):
    c = _get_customer_by_key(x_api_key or "")
    if not c: raise HTTPException(401, "Invalid API key")
    return [{"domain": d, "status": v["status"], "routing_mode": v["routing_mode"]}
            for d, v in _domains.items() if v["customer_id"] == c["id"]]

@app.get("/api/domains/{domain}")
async def api_get_domain(domain: str, x_api_key: str = Header(None, alias="X-API-Key")):
    c = _get_customer_by_key(x_api_key or "")
    if not c: raise HTTPException(401, "Invalid API key")
    d = _domains.get(domain)
    if not d: raise HTTPException(404, "Domain not found")
    if d["customer_id"] != c["id"]: raise HTTPException(403, "Forbidden")
    return d

@app.post("/api/translation/{domain}/config")
async def api_save_config(domain: str, request: Request,
                           x_api_key: str = Header(None, alias="X-API-Key")):
    c = _get_customer_by_key(x_api_key or "")
    if not c: raise HTTPException(401, "Invalid API key")
    d = _domains.get(domain)
    if not d: raise HTTPException(404, "Domain not found")
    if d["customer_id"] != c["id"]: raise HTTPException(403, "Forbidden")
    body = await request.json()
    d["config"] = body
    # Clear page cache on config change
    keys_to_del = [k for k in _pages if k[0] == domain]
    for k in keys_to_del: del _pages[k]
    return {"id": str(uuid.uuid4()), "domain_id": d["id"],
            "languages": body.get("languages", "DE"),
            "frequency": body.get("frequency", "manual"),
            "routing_mode": body.get("routing_mode", "subdirectory"),
            "extra_urls": body.get("extra_urls"), "last_crawl": None,
            "next_crawl": datetime.now(timezone.utc).isoformat(),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat()}

@app.post("/api/translation/{domain}/crawl-now")
async def api_crawl_now(domain: str, x_api_key: str = Header(None, alias="X-API-Key")):
    c = _get_customer_by_key(x_api_key or "")
    if not c: raise HTTPException(401, "Invalid API key")
    d = _domains.get(domain)
    if not d: raise HTTPException(404, "Domain not found")
    # Simulate crawl — translate all pages in all languages
    cfg   = d.get("config", {})
    langs = [l.strip().upper() for l in cfg.get("languages", "DE").split(",") if l.strip()]
    routing = d.get("routing_mode", "subdirectory")
    total_words = 0
    ok = 0
    for path, html in ORIGIN_HTML.items():
        for lang in langs:
            translated = _translate_html(html, lang, domain, langs, routing)
            _pages[(domain, lang, path)] = translated
            total_words += _words(html) // 2  # rough estimate
            ok += 1
    cid = c["id"]
    _usage.setdefault(cid, {"words": 0, "pages": 0})
    _usage[cid]["words"] += total_words
    return {"domain": domain, "urls": len(ORIGIN_HTML), "ok": ok, "failed": 0,
            "langs": langs, "words_translated": total_words,
            "message": f"Crawl complete: {ok} pages translated, {total_words} words sent to DeepL."}

@app.get("/api/translation/{domain}/glossary")
async def api_get_glossary(domain: str, x_api_key: str = Header(None, alias="X-API-Key")):
    c = _get_customer_by_key(x_api_key or "")
    if not c: raise HTTPException(401, "Invalid API key")
    d = _domains.get(domain)
    if not d: raise HTTPException(404, "Domain not found")
    if d["customer_id"] != c["id"]: raise HTTPException(403, "Forbidden")
    return _glossary.get(domain, [])

@app.post("/api/translation/{domain}/glossary")
async def api_add_glossary(domain: str, request: Request,
                            x_api_key: str = Header(None, alias="X-API-Key")):
    c = _get_customer_by_key(x_api_key or "")
    if not c: raise HTTPException(401, "Invalid API key")
    d = _domains.get(domain)
    if not d: raise HTTPException(404, "Domain not found")
    if d["customer_id"] != c["id"]: raise HTTPException(403, "Forbidden")
    body = await request.json()
    rule = {"id": str(uuid.uuid4()), "domain_id": d["id"],
            "source_term": body["source_term"], "replacement": body.get("replacement"),
            "language": body.get("language"), "case_sensitive": body.get("case_sensitive", False),
            "created_at": datetime.now(timezone.utc).isoformat()}
    _glossary.setdefault(domain, []).append(rule)
    # Clear page cache
    for k in list(_pages): 
        if k[0] == domain: del _pages[k]
    return JSONResponse(rule, 201)

@app.delete("/api/translation/{domain}/glossary/{rule_id}")
async def api_delete_glossary(domain: str, rule_id: str,
                               x_api_key: str = Header(None, alias="X-API-Key")):
    c = _get_customer_by_key(x_api_key or "")
    if not c: raise HTTPException(401, "Invalid API key")
    rules = _glossary.get(domain, [])
    new_rules = [r for r in rules if r["id"] != rule_id]
    if len(new_rules) == len(rules): raise HTTPException(404, "Rule not found")
    _glossary[domain] = new_rules
    return JSONResponse(None, 204)

@app.get("/api/system/translation-memory/stats")
async def api_tm_stats(x_api_key: str = Header(None, alias="X-API-Key")):
    c = _get_customer_by_key(x_api_key or "")
    if not c: raise HTTPException(401, "Invalid API key")
    by_lang = {}
    for (h, lang), _ in _tm.items():
        by_lang[lang] = by_lang.get(lang, 0) + 1
    return {"total_entries": len(_tm), "total_cache_hits": sum(v for v in by_lang.values()),
            "hot_cache_size": len(_tm), "by_language": by_lang}

@app.get("/api/usage")
async def api_usage(x_api_key: str = Header(None, alias="X-API-Key")):
    c = _get_customer_by_key(x_api_key or "")
    if not c: raise HTTPException(401, "Invalid API key")
    u = _usage.get(c["id"], {"words": 0, "pages": 0})
    limit = PLAN_LIMITS.get(c["plan"], 2000)
    used  = u["words"]
    return {"year": datetime.now().year, "month": datetime.now().month,
            "words_translated": used, "pages_served": u["pages"], "crawls_run": 0,
            "plan": c["plan"], "word_limit": limit, "word_limit_unlimited": False,
            "word_percent_used": round(used/limit*100, 1) if limit > 0 else 0,
            "over_limit": used >= limit}

@app.get("/api/nginx/status")
async def api_nginx(_: str = Header(None, alias="X-API-Key")):
    return {"nginx_ok": True, "active_domains": len(_domains),
            "detail": "Demo mode — nginx not running (all routing handled by demo server)"}

@app.get("/api/health")
async def api_health():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat(),
            "mode": "demo", "domains": len(_domains), "tm_entries": len(_tm)}

if __name__ == "__main__":
    import uvicorn
    print()
    print("=" * 60)
    print("  🔀 Translation Proxy — Live Browser Demo")
    print("=" * 60)
    print()
    print("  Open: http://localhost:8888")
    print()
    print("  Quick test flow:")
    print("  1. Setup tab → Create Account")
    print("  2. Setup tab → Register Domain")
    print("  3. Setup tab → Save Config (set languages: DE,FR,ES)")
    print("  4. Translate tab → click any language preview link")
    print("  5. Glossary tab → add 'DemoShop' as do-not-translate")
    print("  6. TM & Usage tab → see cache stats grow")
    print()
    print("  Press Ctrl+C to stop")
    print()
    uvicorn.run(app, host="0.0.0.0", port=8888, log_level="warning")
