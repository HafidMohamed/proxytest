"""
Translation Proxy – Control Plane API  (v2)
============================================

New in v2:
  - API key hashing (SHA-256 stored, raw key returned once)
  - Rate limiting via slowapi
  - Glossary CRUD endpoints
  - Translation Memory stats + management
  - Usage / billing summary endpoint
  - Language update endpoint (regenerates nginx config)
  - CDN status endpoint
  - Routing mode (subdirectory vs subdomain) support throughout
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Depends, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from sqlalchemy.orm import Session

from .config   import settings
from .database import engine, Base, get_db, SessionLocal
from .models   import (
    Customer, Domain, DomainStatus, SSLStatus, ProxyRequestLog,
    TranslationConfig, TranslatedPage, CrawlFrequency, CrawlStatus,
    GlossaryRule, RoutingMode,
)
from .schemas  import (
    CustomerCreate, CustomerResponse, CustomerCreatedResponse,
    DomainCreate, DomainResponse, DomainVerificationInstructions,
    MessageResponse, NginxStatusResponse,
    TranslationConfigCreate, TranslationConfigResponse,
    TranslatedPageSummary, CrawlSummaryResponse,
    GlossaryRuleCreate, GlossaryRuleResponse,
    UsageSummaryResponse, TranslationMemoryStats,
)
from .services.nginx_manager      import NginxManager
from .services.cloudflare_manager import CloudflareManager
from .services.seo_crawler        import run_crawl, discover_urls, next_crawl_time
from .services.scheduler          import start_scheduler, stop_scheduler
from .services.auth               import (
    generate as generate_api_key,
    hash_key, verify as verify_api_key,
    RATE_CUSTOMER_CREATE, RATE_DOMAIN_REGISTER, RATE_VERIFY,
    RATE_PROVISION_SSL, RATE_CRAWL_NOW, RATE_GLOBAL_PER_IP,
)
from .services import (
    full_domain_check,
    issue_certificate, pre_issue_checks,
    cert_exists, cert_paths, get_cert_expiry,
    revoke_and_delete_certificate, renew_all_certificates,
    tm_stats, get_usage_summary, is_over_word_limit,
    is_cdn_enabled,
)

Base.metadata.create_all(bind=engine)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
)
logger = logging.getLogger(__name__)


def _to_uuid(val):
    """Convert str or UUID to uuid.UUID — works with both SQLite (test) and PostgreSQL."""
    if isinstance(val, __import__('uuid').UUID):
        return val
    return __import__('uuid').UUID(str(val))

# ── Rate limiter ──────────────────────────────────────────────────────────────

limiter = Limiter(key_func=get_remote_address, default_limits=[RATE_GLOBAL_PER_IP])

app = FastAPI(
    title="Translation Proxy – Control Plane",
    version="2.0.0",
    description=(
        "Manages customer domains, DNS verification, SSL certificates, "
        "Nginx proxy configs, Cloudflare IP protection, translation memory, "
        "glossary rules, and metered usage billing."
    ),
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key"],
    allow_credentials=True,
)

nginx_mgr = NginxManager()
cf_mgr    = CloudflareManager()


@app.on_event("startup")
async def _startup():
    start_scheduler(
        db_session_factory=SessionLocal,
        deepl_api_key=settings.DEEPL_API_KEY,
    )
    logger.info("Translation scheduler started")


@app.on_event("shutdown")
async def _shutdown():
    stop_scheduler()


# ── Auth ──────────────────────────────────────────────────────────────────────

def require_api_key(
    x_api_key: str = Header(..., alias="X-API-Key"),
    db: Session = Depends(get_db),
) -> Customer:
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Missing X-API-Key header")
    key_hash = hash_key(x_api_key)
    customer = db.query(Customer).filter(Customer.api_key_hash == key_hash).first()
    if not customer or not customer.is_active:
        raise HTTPException(status_code=401, detail="Invalid or inactive API key")
    return customer


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health", tags=["System"])
def health():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


# ── Customers ─────────────────────────────────────────────────────────────────

@app.post("/customers", response_model=CustomerCreatedResponse, status_code=201, tags=["Customers"])
@limiter.limit(RATE_CUSTOMER_CREATE)
def create_customer(request: Request, payload: CustomerCreate, db: Session = Depends(get_db)):
    """
    Create a new customer account.
    Returns the raw API key **once** — store it securely, it cannot be retrieved again.
    """
    if db.query(Customer).filter(Customer.email == payload.email).first():
        raise HTTPException(status_code=409, detail="Email already registered")

    raw_key, key_hash, key_prefix = generate_api_key()

    customer = Customer(
        id             = uuid.uuid4(),
        email          = payload.email,
        api_key_hash   = key_hash,
        api_key_prefix = key_prefix,
        plan           = "free",
    )
    db.add(customer)
    db.commit()
    db.refresh(customer)

    # Build response dict — include raw_api_key which is NOT on the model
    return CustomerCreatedResponse(
        id             = customer.id,
        email          = customer.email,
        api_key_prefix = customer.api_key_prefix,
        is_active      = customer.is_active,
        plan           = customer.plan,
        created_at     = customer.created_at,
        raw_api_key    = raw_key,
    )


@app.get("/customers/me", response_model=CustomerResponse, tags=["Customers"])
def get_me(customer: Customer = Depends(require_api_key)):
    return customer


# ── Domains ───────────────────────────────────────────────────────────────────

@app.post("/domains", response_model=DomainVerificationInstructions,
          status_code=201, tags=["Domains"])
@limiter.limit(RATE_DOMAIN_REGISTER)
def register_domain(
    request: Request,
    payload: DomainCreate,
    customer: Customer = Depends(require_api_key),
    db: Session = Depends(get_db),
):
    existing = db.query(Domain).filter(Domain.domain == payload.domain).first()
    if existing:
        if str(existing.customer_id) != str(customer.id):
            raise HTTPException(status_code=409, detail="Domain already registered to another customer")
        return _build_instructions(existing)

    import secrets
    token = secrets.token_urlsafe(24)
    domain_obj = Domain(
        id=uuid.uuid4(), customer_id=customer.id, domain=payload.domain,
        backend_url=payload.backend_url, verification_token=token,
        routing_mode=payload.routing_mode,
        status=DomainStatus.PENDING_VERIFICATION, ssl_status=SSLStatus.PENDING,
    )
    db.add(domain_obj)
    db.commit()
    db.refresh(domain_obj)

    try:
        nginx_mgr.write_http_only_config(payload.domain)
        nginx_mgr.reload()
    except Exception as exc:
        logger.warning("Could not write http-only config for %s: %s", payload.domain, exc)

    return _build_instructions(domain_obj)


def _build_instructions(d: Domain) -> DomainVerificationInstructions:
    txt_name  = f"{settings.VERIFICATION_TXT_PREFIX}.{d.domain}"
    txt_value = f"proxy-verify={d.verification_token}"
    return DomainVerificationInstructions(
        domain=d.domain,
        txt_record_name=txt_name,
        txt_record_value=txt_value,
        a_record_value=settings.OUR_SERVER_IP,
        instructions=(
            f"  TYPE  NAME                        VALUE\n"
            f"  TXT   {txt_name}  \"{txt_value}\"\n"
            f"  A     {d.domain}  {settings.OUR_SERVER_IP}\n\n"
            f"  If using subdomain routing, also add CNAME records:\n"
            f"  CNAME de.{d.domain}  {d.domain}\n"
            f"  CNAME fr.{d.domain}  {d.domain}\n"
            f"  (one per language you configure)\n"
        ),
    )


@app.get("/domains", response_model=List[DomainResponse], tags=["Domains"])
def list_domains(customer: Customer = Depends(require_api_key), db: Session = Depends(get_db)):
    return db.query(Domain).filter(Domain.customer_id == customer.id).all()


@app.get("/domains/{domain}", response_model=DomainResponse, tags=["Domains"])
def get_domain(domain: str, customer: Customer = Depends(require_api_key), db: Session = Depends(get_db)):
    return _get_owned_domain(domain, customer, db)


@app.post("/domains/{domain}/verify", response_model=MessageResponse, tags=["Domains"])
@limiter.limit(RATE_VERIFY)
def verify_domain(
    request: Request, domain: str,
    customer: Customer = Depends(require_api_key), db: Session = Depends(get_db),
):
    obj = _get_owned_domain(domain, customer, db)
    if obj.is_verified:
        return MessageResponse(message="Domain already verified", detail=str(obj.verified_at))

    all_ok, details = full_domain_check(domain, obj.verification_token)
    _log(db, domain, "dns_check", str(details))

    if not all_ok:
        return MessageResponse(message="DNS verification failed", detail=str(details))

    obj.is_verified = True
    obj.verified_at = datetime.now(timezone.utc)
    obj.status      = DomainStatus.VERIFIED
    db.commit()
    return MessageResponse(message="Domain verified.", detail=str(details))


@app.post("/domains/{domain}/provision-ssl", response_model=MessageResponse, tags=["Domains"])
@limiter.limit(RATE_PROVISION_SSL)
def provision_ssl(
    request: Request, domain: str,
    customer: Customer = Depends(require_api_key), db: Session = Depends(get_db),
):
    obj = _get_owned_domain(domain, customer, db)
    if not obj.is_verified:
        raise HTTPException(status_code=400, detail="Domain must be verified before SSL can be provisioned")
    if obj.ssl_status == SSLStatus.ACTIVE and cert_exists(domain):
        return MessageResponse(message="SSL certificate already active")

    obj.ssl_status = SSLStatus.PENDING
    db.commit()

    pre_ok, pre_msg = pre_issue_checks(domain)
    if not pre_ok:
        obj.ssl_status = SSLStatus.FAILED
        db.commit()
        _log(db, domain, "ssl_preflight_failed", pre_msg)
        raise HTTPException(status_code=400, detail=f"ACME pre-flight check failed: {pre_msg}")

    try:
        success, msg = issue_certificate(domain)
    except Exception as exc:
        logger.exception("Unexpected error during cert issuance for %s", domain)
        obj.ssl_status = SSLStatus.FAILED
        db.commit()
        _log(db, domain, "ssl_exception", str(exc))
        raise HTTPException(status_code=500, detail=f"Unexpected error during SSL issuance: {exc}")

    if not success:
        obj.ssl_status = SSLStatus.FAILED
        db.commit()
        _log(db, domain, "ssl_failed", msg)
        raise HTTPException(status_code=500, detail=f"SSL issuance failed: {msg}")

    fullchain, privkey = cert_paths(domain)
    expires_at = get_cert_expiry(domain)

    # Get languages from translation config if it exists
    cfg = db.query(TranslationConfig).filter(TranslationConfig.domain_id == obj.id).first()
    languages  = [l.strip().upper() for l in (cfg.languages if cfg else "").split(",") if l.strip()] or ["DE"]
    routing    = cfg.routing_mode.value if cfg and cfg.routing_mode else obj.routing_mode.value

    ok, config_path, reload_msg = nginx_mgr.safe_write_and_reload(
        domain=domain, backend_url=obj.backend_url,
        ssl_cert=fullchain, ssl_key=privkey,
        languages=languages, routing_mode=routing,
    )
    if not ok:
        obj.status = DomainStatus.FAILED
        db.commit()
        _log(db, domain, "nginx_failed", reload_msg)
        raise HTTPException(status_code=500, detail=f"Nginx config failed: {reload_msg}")

    obj.ssl_status        = SSLStatus.ACTIVE
    obj.ssl_cert_path     = fullchain
    obj.ssl_key_path      = privkey
    obj.ssl_issued_at     = datetime.now(timezone.utc)
    obj.ssl_expires_at    = expires_at
    obj.status            = DomainStatus.ACTIVE
    obj.nginx_config_path = config_path
    db.commit()

    _log(db, domain, "ssl_issued", f"cert={fullchain} expires={expires_at}")
    return MessageResponse(
        message="SSL certificate issued and nginx configured. Domain is live!",
        detail=f"Certificate expires: {expires_at}. Routing mode: {routing}. Languages: {languages}.",
    )


@app.put("/domains/{domain}/backend", response_model=MessageResponse, tags=["Domains"])
def update_backend(
    domain: str, payload: DomainCreate,
    customer: Customer = Depends(require_api_key), db: Session = Depends(get_db),
):
    obj = _get_owned_domain(domain, customer, db)
    if obj.status != DomainStatus.ACTIVE:
        raise HTTPException(status_code=400, detail="Domain must be active to update backend")

    old_backend  = obj.backend_url
    obj.backend_url = payload.backend_url
    db.commit()

    if obj.ssl_cert_path and obj.ssl_key_path:
        cfg      = db.query(TranslationConfig).filter(TranslationConfig.domain_id == obj.id).first()
        languages = [l.strip().upper() for l in (cfg.languages if cfg else "").split(",") if l.strip()] or ["DE"]
        routing   = cfg.routing_mode.value if cfg and cfg.routing_mode else obj.routing_mode.value

        ok, _, msg = nginx_mgr.safe_write_and_reload(
            domain=domain, backend_url=payload.backend_url,
            ssl_cert=obj.ssl_cert_path, ssl_key=obj.ssl_key_path,
            languages=languages, routing_mode=routing,
        )
        if not ok:
            obj.backend_url = old_backend
            db.commit()
            raise HTTPException(status_code=500, detail=f"Nginx reload failed: {msg}")

    _log(db, domain, "backend_updated", f"{old_backend} → {payload.backend_url}")
    return MessageResponse(message="Backend URL updated and nginx reloaded",
                           detail=f"Now proxying to: {payload.backend_url}")


@app.delete("/domains/{domain}", status_code=204, tags=["Domains"])
def delete_domain(
    domain: str, customer: Customer = Depends(require_api_key), db: Session = Depends(get_db),
):
    obj = _get_owned_domain(domain, customer, db)
    nginx_mgr.remove_domain_config(domain)
    nginx_mgr.reload()
    if obj.ssl_status == SSLStatus.ACTIVE:
        try:
            revoke_and_delete_certificate(domain)
        except Exception as exc:
            logger.warning("Could not revoke cert for %s: %s", domain, exc)
    # Clean up CDN objects for this domain
    from .services.cdn_storage import delete_domain as cdn_del
    try:
        cdn_del(domain)
    except Exception:
        pass
    db.delete(obj)
    db.commit()


# ── Nginx ─────────────────────────────────────────────────────────────────────

@app.get("/nginx/status", response_model=NginxStatusResponse, tags=["Nginx"])
def nginx_status():
    ok, msg = nginx_mgr.test_config()
    return NginxStatusResponse(nginx_ok=ok, active_domains=len(nginx_mgr.list_active_domains()), detail=msg)


@app.post("/nginx/reload", response_model=MessageResponse, tags=["Nginx"])
def nginx_reload():
    ok, msg = nginx_mgr.reload()
    if not ok:
        raise HTTPException(status_code=500, detail=msg)
    return MessageResponse(message="Nginx reloaded", detail=msg)


# ── SSL ───────────────────────────────────────────────────────────────────────

@app.post("/ssl/renew-all", response_model=MessageResponse, tags=["SSL"])
def ssl_renew_all():
    result = renew_all_certificates()
    return MessageResponse(
        message="Renewal complete" if result["success"] else "Renewal had errors",
        detail=result["stdout"] or result["stderr"],
    )


# ── Cloudflare ────────────────────────────────────────────────────────────────

@app.get("/cloudflare/status", tags=["Cloudflare"])
def cloudflare_status():
    return cf_mgr.get_status()


@app.post("/cloudflare/refresh", tags=["Cloudflare"])
def cloudflare_refresh(update_ufw: bool = True):
    result = cf_mgr.full_refresh(update_ufw=update_ufw)
    ok, msg = nginx_mgr.reload()
    result["nginx_reloaded"] = ok
    result["nginx_message"]  = msg
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# TRANSLATION CONFIG
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/translation/{domain}/config", response_model=TranslationConfigResponse, tags=["Translation"])
def upsert_translation_config(
    domain: str, payload: TranslationConfigCreate,
    customer: Customer = Depends(require_api_key), db: Session = Depends(get_db),
):
    obj = _get_owned_domain(domain, customer, db)
    cfg = db.query(TranslationConfig).filter(TranslationConfig.domain_id == obj.id).first()
    now = datetime.now(timezone.utc)

    if cfg:
        cfg.languages    = payload.languages
        cfg.frequency    = payload.frequency
        cfg.extra_urls   = payload.extra_urls
        cfg.routing_mode = payload.routing_mode
        cfg.updated_at   = now
        cfg.next_crawl   = now
    else:
        cfg = TranslationConfig(
            domain_id    = obj.id,
            languages    = payload.languages,
            frequency    = payload.frequency,
            extra_urls   = payload.extra_urls,
            routing_mode = payload.routing_mode,
            next_crawl   = now,
        )
        db.add(cfg)

    db.commit()
    db.refresh(cfg)

    # Regenerate nginx config with new languages if domain is active
    if obj.status == DomainStatus.ACTIVE and obj.ssl_cert_path:
        langs = [l.strip().upper() for l in payload.languages.split(",") if l.strip()]
        ok, msg = nginx_mgr.update_languages(
            domain=domain, backend_url=obj.backend_url,
            ssl_cert=obj.ssl_cert_path, ssl_key=obj.ssl_key_path,
            languages=langs, routing_mode=payload.routing_mode.value,
        )
        if not ok:
            logger.warning("nginx update_languages failed: %s", msg)

    return cfg


@app.get("/translation/{domain}/config", response_model=TranslationConfigResponse, tags=["Translation"])
def get_translation_config(
    domain: str, customer: Customer = Depends(require_api_key), db: Session = Depends(get_db),
):
    obj = _get_owned_domain(domain, customer, db)
    cfg = db.query(TranslationConfig).filter(TranslationConfig.domain_id == obj.id).first()
    if not cfg:
        raise HTTPException(status_code=404, detail="No translation config for this domain")
    return cfg


@app.delete("/translation/{domain}/config", status_code=204, tags=["Translation"])
def delete_translation_config(
    domain: str, customer: Customer = Depends(require_api_key), db: Session = Depends(get_db),
):
    obj = _get_owned_domain(domain, customer, db)
    cfg = db.query(TranslationConfig).filter(TranslationConfig.domain_id == obj.id).first()
    if cfg:
        db.delete(cfg)
        db.commit()


@app.get("/translation/{domain}/pages", response_model=List[TranslatedPageSummary], tags=["Translation"])
def list_translated_pages(
    domain: str, lang: Optional[str] = None, status: Optional[str] = None,
    limit: int = 100, offset: int = 0,
    customer: Customer = Depends(require_api_key), db: Session = Depends(get_db),
):
    obj = _get_owned_domain(domain, customer, db)
    cfg = db.query(TranslationConfig).filter(TranslationConfig.domain_id == obj.id).first()
    if not cfg:
        return []
    q = db.query(TranslatedPage).filter(TranslatedPage.config_id == cfg.id)
    if lang:
        q = q.filter(TranslatedPage.language == lang.upper())
    if status:
        q = q.filter(TranslatedPage.status == status)
    return q.order_by(TranslatedPage.url).offset(offset).limit(limit).all()


@app.post("/translation/{domain}/crawl-now", response_model=CrawlSummaryResponse, tags=["Translation"])
@limiter.limit(RATE_CRAWL_NOW)
async def crawl_now(
    request: Request, domain: str,
    customer: Customer = Depends(require_api_key), db: Session = Depends(get_db),
):
    """Immediately crawl + translate. Blocks until complete."""
    if is_over_word_limit(db, str(customer.id), customer.plan):
        raise HTTPException(status_code=402,
                            detail="Monthly word limit exceeded. Upgrade your plan.")

    obj = _get_owned_domain(domain, customer, db)
    cfg = db.query(TranslationConfig).filter(TranslationConfig.domain_id == obj.id).first()
    if not cfg:
        raise HTTPException(status_code=404,
                            detail="No translation config. Call POST /translation/{domain}/config first.")

    langs      = [l.strip().upper() for l in cfg.languages.split(",") if l.strip()]
    extra_urls = [u.strip() for u in (cfg.extra_urls or "").splitlines() if u.strip()]
    routing    = cfg.routing_mode.value if cfg.routing_mode else "subdirectory"

    summary = await run_crawl(
        config_id          = str(cfg.id),
        domain             = domain,
        domain_id          = str(obj.id),
        customer_id        = str(customer.id),
        backend_url        = obj.backend_url,
        languages          = langs,
        extra_urls         = extra_urls,
        deepl_api_key      = settings.DEEPL_API_KEY,
        db_session_factory = SessionLocal,
        routing_mode       = routing,
    )

    return CrawlSummaryResponse(
        **{k: summary[k] for k in ("domain", "urls", "ok", "failed", "langs", "words_translated")},
        message=f"Crawl complete: {summary['ok']} pages translated, {summary['failed']} failed, "
                f"{summary['words_translated']} words sent to DeepL.",
    )


# ═══════════════════════════════════════════════════════════════════════════════
# GLOSSARY
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/translation/{domain}/glossary", response_model=List[GlossaryRuleResponse], tags=["Glossary"])
def list_glossary(
    domain: str, customer: Customer = Depends(require_api_key), db: Session = Depends(get_db),
):
    obj = _get_owned_domain(domain, customer, db)
    return db.query(GlossaryRule).filter(GlossaryRule.domain_id == obj.id).all()


@app.post("/translation/{domain}/glossary", response_model=GlossaryRuleResponse,
          status_code=201, tags=["Glossary"])
def add_glossary_rule(
    domain: str, payload: GlossaryRuleCreate,
    customer: Customer = Depends(require_api_key), db: Session = Depends(get_db),
):
    """
    Add a glossary rule.
    - `replacement=null` → term is never translated (kept as-is in all languages)
    - `replacement="Farbe"` → term is always replaced with this value (for language=DE)
    - `language=null` → rule applies to all languages
    """
    obj = _get_owned_domain(domain, customer, db)
    rule = GlossaryRule(
        id             = uuid.uuid4(),
        domain_id      = obj.id,
        source_term    = payload.source_term,
        language       = payload.language,
        replacement    = payload.replacement,
        case_sensitive = payload.case_sensitive,
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule


@app.delete("/translation/{domain}/glossary/{rule_id}", status_code=204, tags=["Glossary"])
def delete_glossary_rule(
    domain: str, rule_id: str,
    customer: Customer = Depends(require_api_key), db: Session = Depends(get_db),
):
    obj = _get_owned_domain(domain, customer, db)
    rule = db.query(GlossaryRule).filter(
        GlossaryRule.id == _to_uuid(rule_id), GlossaryRule.domain_id == obj.id
    ).first()
    if not rule:
        raise HTTPException(status_code=404, detail="Glossary rule not found")
    db.delete(rule)
    db.commit()


# ═══════════════════════════════════════════════════════════════════════════════
# TRANSLATION MEMORY
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/system/translation-memory/stats", response_model=TranslationMemoryStats,
         tags=["Translation Memory"])
def translation_memory_stats(
    _: Customer = Depends(require_api_key), db: Session = Depends(get_db),
):
    """Returns TM statistics — shared across all customers."""
    return tm_stats(db)


# ═══════════════════════════════════════════════════════════════════════════════
# USAGE / BILLING
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/usage", response_model=UsageSummaryResponse, tags=["Usage"])
def get_usage(
    year: Optional[int] = None, month: Optional[int] = None,
    customer: Customer = Depends(require_api_key), db: Session = Depends(get_db),
):
    """Monthly usage summary — words translated, pages served, plan limits."""
    return get_usage_summary(db, str(customer.id), customer.plan)


# ═══════════════════════════════════════════════════════════════════════════════
# SERVE TRANSLATED PAGES  (called by translation_worker)
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/translated/{domain}/{lang}/{path:path}", tags=["Translation Serve"], include_in_schema=False)
async def serve_translated_page(domain: str, lang: str, path: str, db: Session = Depends(get_db)):
    from fastapi.responses import HTMLResponse, JSONResponse

    obj = db.query(Domain).filter(Domain.domain == domain).first()
    if not obj:
        raise HTTPException(status_code=404, detail="Domain not found")

    full_url = f"{obj.backend_url.rstrip('/')}/{path.lstrip('/')}"
    cfg = db.query(TranslationConfig).filter(TranslationConfig.domain_id == obj.id).first()
    if not cfg:
        return JSONResponse({"cached": False}, status_code=200)

    page = (
        db.query(TranslatedPage)
        .filter_by(config_id=cfg.id, url=full_url, language=lang.upper())
        .filter(TranslatedPage.status == CrawlStatus.DONE)
        .first()
    )

    if page:
        # Try CDN URL first
        if page.html_url:
            from fastapi.responses import RedirectResponse
            return RedirectResponse(url=page.html_url, status_code=302)
        if page.html:
            return HTMLResponse(
                content=page.html,
                headers={
                    "X-Translation-Source": "precomputed",
                    "X-Translated-To": lang.upper(),
                    "X-Crawled-At": str(page.crawled_at),
                },
            )

    return JSONResponse({"cached": False}, status_code=200)


@app.get("/translated/{domain}/{lang}/sitemap.xml", tags=["Translation Serve"])
async def serve_translated_sitemap(domain: str, lang: str, db: Session = Depends(get_db)):
    from fastapi.responses import Response as FastAPIResponse

    obj = db.query(Domain).filter(Domain.domain == domain).first()
    if not obj:
        raise HTTPException(status_code=404)

    cfg = db.query(TranslationConfig).filter(TranslationConfig.domain_id == obj.id).first()
    pages = []
    if cfg:
        pages = (
            db.query(TranslatedPage)
            .filter_by(config_id=cfg.id, language=lang.upper())
            .filter(TranslatedPage.status == CrawlStatus.DONE)
            .all()
        )

    routing = cfg.routing_mode.value if cfg else "subdirectory"
    all_langs = [l.strip().upper() for l in (cfg.languages if cfg else "").split(",") if l.strip()]

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"',
        '        xmlns:xhtml="http://www.w3.org/1999/xhtml">',
    ]
    for page in pages:
        from urllib.parse import urlparse, urlunparse
        parsed = urlparse(page.url)

        if routing == "subdomain":
            proxy_url = f"https://{lang.lower()}.{domain}{parsed.path}"
        else:
            proxy_url = f"https://{domain}/{lang.lower()}{parsed.path}"

        last_mod = (page.crawled_at or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
        lines += [
            "  <url>",
            f"    <loc>{proxy_url}</loc>",
            f"    <lastmod>{last_mod}</lastmod>",
            f"    <changefreq>weekly</changefreq>",
        ]
        # Add hreflang alternates in sitemap (belt-and-suspenders for Googlebot)
        orig_url = f"https://{domain}{parsed.path}"
        lines.append(f"    <xhtml:link rel='alternate' hreflang='x-default' href='{orig_url}'/>")
        for al in all_langs:
            if routing == "subdomain":
                alt_url = f"https://{al.lower()}.{domain}{parsed.path}"
            else:
                alt_url = f"https://{domain}/{al.lower()}{parsed.path}"
            lines.append(f"    <xhtml:link rel='alternate' hreflang='{al.lower()}' href='{alt_url}'/>")
        lines.append("  </url>")

    lines.append("</urlset>")

    return FastAPIResponse(content="\n".join(lines), media_type="application/xml")


@app.get("/translated/{domain}/robots.txt", tags=["Translation Serve"])
async def serve_translated_robots(domain: str, db: Session = Depends(get_db)):
    from fastapi.responses import PlainTextResponse

    obj = db.query(Domain).filter(Domain.domain == domain).first()
    cfg = None
    if obj:
        cfg = db.query(TranslationConfig).filter(TranslationConfig.domain_id == obj.id).first()

    lines = ["User-agent: *", "Allow: /", ""]

    if cfg:
        langs = [l.strip().upper() for l in cfg.languages.split(",") if l.strip()]
        for lang in langs:
            lines.append(f"Sitemap: https://{domain}/sitemap-{lang.lower()}.xml")

    return PlainTextResponse("\n".join(lines))


# ── Private helpers ───────────────────────────────────────────────────────────

def _get_owned_domain(domain: str, customer: Customer, db: Session) -> Domain:
    obj = db.query(Domain).filter(Domain.domain == domain).first()
    if not obj:
        raise HTTPException(status_code=404, detail="Domain not found")
    if str(obj.customer_id) != str(customer.id):
        raise HTTPException(status_code=403, detail="Forbidden")
    return obj


def _log(db: Session, domain: str, event: str, detail: str):
    db.add(ProxyRequestLog(domain=domain, event=event, detail=detail[:2000]))
    db.commit()
