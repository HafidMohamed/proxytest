"""
Translation Proxy – Control Plane API
======================================

Endpoints
---------
POST   /customers                              Create customer account
GET    /customers/{id}                         Get customer

POST   /domains                                Register domain → DNS instructions
GET    /domains                                List customer domains
GET    /domains/{domain}                       Get domain status
POST   /domains/{domain}/verify                DNS verification check
POST   /domains/{domain}/provision-ssl         Issue SSL + activate nginx config
DELETE /domains/{domain}                       Remove domain

GET    /nginx/status                           Nginx health
POST   /nginx/reload                           Force nginx reload

POST   /ssl/renew-all                          Certbot renew all certs

GET    /cloudflare/status                      CF IP cache info
POST   /cloudflare/refresh                     Fetch fresh CF IPs + update nginx + UFW

GET    /health                                 Liveness check
"""

import logging
import secrets
import uuid
from datetime import datetime, timezone
from typing import List

from fastapi import FastAPI, HTTPException, Depends, Header, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from .config   import settings
from .database import engine, Base, get_db
from .models   import Customer, Domain, DomainStatus, SSLStatus, ProxyRequestLog
from .schemas  import (
    CustomerCreate, CustomerResponse,
    DomainCreate, DomainResponse, DomainVerificationInstructions,
    MessageResponse, NginxStatusResponse,
)
from .services.nginx_manager      import NginxManager
from .services.cloudflare_manager import CloudflareManager
from .services import (
    full_domain_check,
    issue_certificate, pre_issue_checks,
    cert_exists, cert_paths, get_cert_expiry,
    revoke_and_delete_certificate, renew_all_certificates,
)

Base.metadata.create_all(bind=engine)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Translation Proxy – Control Plane",
    version="2.0.0",
    description=(
        "Manages customer domains, DNS verification, SSL certificates, "
        "Nginx proxy configs, and Cloudflare IP protection."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

nginx_mgr = NginxManager()
cf_mgr    = CloudflareManager()


# ── Auth ──────────────────────────────────────────────────────────────────────

def require_api_key(
    x_api_key: str = Header(..., alias="X-API-Key"),
    db: Session = Depends(get_db),
) -> Customer:
    customer = db.query(Customer).filter(Customer.api_key == x_api_key).first()
    if not customer or not customer.is_active:
        raise HTTPException(status_code=401, detail="Invalid or inactive API key")
    return customer


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health", tags=["System"])
def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


@app.get("/debug/domain/{domain}", tags=["System"])
def debug_domain(domain: str, db: Session = Depends(get_db)):
    """
    Diagnose the full proxy setup for a domain.
    Shows exactly what nginx config exists and what the proxy will do.
    Remove or restrict this endpoint in production.
    """
    import subprocess, os
    from pathlib import Path

    obj = db.query(Domain).filter(Domain.domain == domain).first()

    # Check nginx config files
    sites_enabled  = Path(settings.NGINX_SITES_ENABLED)
    conf_path      = sites_enabled / f"{domain}.conf"
    http_conf_path = sites_enabled / f"{domain}.http.conf"

    nginx_conf_exists  = conf_path.exists()
    nginx_http_exists  = http_conf_path.exists()
    nginx_conf_content = conf_path.read_text() if nginx_conf_exists else None

    # Check cert files
    cert_readable = False
    cert_expiry   = None
    if obj and obj.ssl_cert_path:
        try:
            Path(obj.ssl_cert_path).read_bytes()
            cert_readable = True
            from .services.ssl_manager import get_cert_expiry
            cert_expiry = str(get_cert_expiry(domain))
        except Exception as e:
            cert_readable = False

    # Check nginx is actually serving it
    nginx_test = subprocess.run(
        ["sudo", "nginx", "-T"],
        capture_output=True, text=True, timeout=10
    )
    domain_in_nginx = domain in nginx_test.stdout

    return {
        "domain": domain,
        "in_database": obj is not None,
        "db_status": obj.status if obj else None,
        "db_ssl_status": obj.ssl_status if obj else None,
        "db_backend_url": obj.backend_url if obj else None,
        "db_verified": obj.is_verified if obj else None,
        "nginx_ssl_config_exists": nginx_conf_exists,
        "nginx_http_config_exists": nginx_http_exists,
        "nginx_knows_domain": domain_in_nginx,
        "cert_path": obj.ssl_cert_path if obj else None,
        "cert_readable_by_app": cert_readable,
        "cert_expiry": cert_expiry,
        "nginx_conf_preview": nginx_conf_content[:500] if nginx_conf_content else None,
        "action_needed": (
            "provision-ssl" if obj and not nginx_conf_exists else
            "verify domain first" if obj and not obj.is_verified else
            "register domain first" if not obj else
            "looks good - check Cloudflare proxy is enabled (orange cloud)"
        )
    }


# ── Customers ─────────────────────────────────────────────────────────────────

@app.post("/customers", response_model=CustomerResponse, status_code=201, tags=["Customers"])
def create_customer(payload: CustomerCreate, db: Session = Depends(get_db)):
    if db.query(Customer).filter(Customer.email == payload.email).first():
        raise HTTPException(status_code=409, detail="Email already registered")
    customer = Customer(
        id=uuid.uuid4(),
        email=payload.email,
        api_key=secrets.token_urlsafe(32),
    )
    db.add(customer)
    db.commit()
    db.refresh(customer)
    return customer


@app.get("/customers/{customer_id}", response_model=CustomerResponse, tags=["Customers"])
def get_customer(customer_id: str, customer: Customer = Depends(require_api_key)):
    if str(customer.id) != customer_id:
        raise HTTPException(status_code=403, detail="Forbidden")
    return customer


# ── Domains ───────────────────────────────────────────────────────────────────

@app.post("/domains", response_model=DomainVerificationInstructions,
          status_code=201, tags=["Domains"])
def register_domain(
    payload: DomainCreate,
    customer: Customer = Depends(require_api_key),
    db: Session = Depends(get_db),
):
    """
    Register a new domain.
    Returns the DNS records the customer must add:
      1. TXT  _proxy-verify.<domain>  "proxy-verify=<token>"  – ownership proof
      2. A    <domain>                <our_server_ip>          – point traffic to us
    """
    existing = db.query(Domain).filter(Domain.domain == payload.domain).first()
    if existing:
        if str(existing.customer_id) != str(customer.id):
            raise HTTPException(
                status_code=409,
                detail="Domain already registered to another customer",
            )
        return _build_instructions(existing)

    token = secrets.token_urlsafe(24)
    domain_obj = Domain(
        id=uuid.uuid4(),
        customer_id=customer.id,
        domain=payload.domain,
        backend_url=payload.backend_url,
        verification_token=token,
        status=DomainStatus.PENDING_VERIFICATION,
        ssl_status=SSLStatus.PENDING,
    )
    db.add(domain_obj)
    db.commit()
    db.refresh(domain_obj)

    # Write HTTP-only nginx config so ACME challenge works as soon as DNS propagates
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
            
            f"  TYPE  NAME                VALUE\n"
            f"  TXT   {txt_name}  \"{txt_value}\"\n"
            f"  A     {d.domain}  {settings.OUR_SERVER_IP}\n\n"
        ),
    )


@app.get("/domains", response_model=List[DomainResponse], tags=["Domains"])
def list_domains(
    customer: Customer = Depends(require_api_key),
    db: Session = Depends(get_db),
):
    return db.query(Domain).filter(Domain.customer_id == customer.id).all()


@app.get("/domains/{domain}", response_model=DomainResponse, tags=["Domains"])
def get_domain(
    domain: str,
    customer: Customer = Depends(require_api_key),
    db: Session = Depends(get_db),
):
    return _get_owned_domain(domain, customer, db)


@app.post("/domains/{domain}/verify", response_model=MessageResponse, tags=["Domains"])
def verify_domain(
    domain: str,
    customer: Customer = Depends(require_api_key),
    db: Session = Depends(get_db),
):
    obj = _get_owned_domain(domain, customer, db)
    if obj.is_verified:
        return MessageResponse(message="Domain already verified", detail=str(obj.verified_at))

    all_ok, details = full_domain_check(domain, obj.verification_token)
    _log(db, domain, "dns_check", str(details))

    if not all_ok:
        return MessageResponse(message="DNS verification failed", detail=str(details))

    obj.is_verified = True
    obj.verified_at = datetime.utcnow()
    obj.status      = DomainStatus.VERIFIED
    db.commit()
    return MessageResponse(
        message="Domain verified.",
        detail=str(details),
    )


@app.post("/domains/{domain}/provision-ssl", response_model=MessageResponse, tags=["Domains"])
def provision_ssl(
    domain: str,
    customer: Customer = Depends(require_api_key),
    db: Session = Depends(get_db),
):
    """
    Issue a Let's Encrypt certificate and activate the full HTTPS nginx config.

    Flow:
      1. certbot webroot → LE issues cert → saved to /etc/letsencrypt/live/<domain>/
      2. Nginx SSL vhost written with:
           - Cloudflare IP allowlist (non-CF traffic silently dropped)
           - CF-Connecting-IP header unwrapped to real visitor IP
           - proxy_pass → customer's backend_url (from DB)
      3. nginx -t + nginx -s reload (graceful, zero downtime)
    """
    obj = _get_owned_domain(domain, customer, db)
    if not obj.is_verified:
        raise HTTPException(
            status_code=400,
            detail="Domain must be verified before SSL can be provisioned",
        )
    if obj.ssl_status == SSLStatus.ACTIVE and cert_exists(domain):
        return MessageResponse(message="SSL certificate already active")

    obj.ssl_status = SSLStatus.PENDING
    db.commit()

    # Pre-flight: verify port 80 is reachable and nginx is serving the ACME webroot
    # before invoking certbot (gives a clear error instead of a cryptic certbot failure)
    pre_ok, pre_msg = pre_issue_checks(domain)
    if not pre_ok:
        obj.ssl_status = SSLStatus.FAILED
        db.commit()
        _log(db, domain, "ssl_preflight_failed", pre_msg)
        raise HTTPException(
            status_code=400,
            detail=(
                f"ACME pre-flight check failed: {pre_msg}\n\n"
                "Fix: make sure port 80 is open to ALL IPs (not just Cloudflare). "
                "Let\'s Encrypt\'s validation servers must reach "
                f"http://{domain}/.well-known/acme-challenge/ "
                "Run: ufw allow 80/tcp && nginx -s reload"
            )
        )

    # BUG FIX: wrap issue_certificate in try/except.
    # Previously, if certbot was missing (FileNotFoundError) or any other
    # unhandled exception occurred, uvicorn would close the connection without
    # sending an HTTP response, leaving clients with an empty body and
    # "Expecting value: line 1 column 1 (char 0)" from json.tool.
    try:
        success, msg = issue_certificate(domain)
    except Exception as exc:
        logger.exception("Unexpected error during cert issuance for %s", domain)
        obj.ssl_status = SSLStatus.FAILED
        db.commit()
        _log(db, domain, "ssl_exception", str(exc))
        raise HTTPException(
            status_code=500,
            detail=f"Unexpected error during SSL issuance: {exc}",
        )
    if not success:
        obj.ssl_status = SSLStatus.FAILED
        db.commit()
        _log(db, domain, "ssl_failed", msg)
        raise HTTPException(status_code=500, detail=f"SSL issuance failed: {msg}")

    fullchain, privkey = cert_paths(domain)
    expires_at = get_cert_expiry(domain)

    ok, config_path, reload_msg = nginx_mgr.safe_write_and_reload(
        domain=domain,
        backend_url=obj.backend_url,
        ssl_cert=fullchain,
        ssl_key=privkey,
    )
    if not ok:
        obj.status = DomainStatus.FAILED
        db.commit()
        _log(db, domain, "nginx_failed", reload_msg)
        raise HTTPException(status_code=500, detail=f"Nginx config failed: {reload_msg}")

    obj.ssl_status        = SSLStatus.ACTIVE
    obj.ssl_cert_path     = fullchain
    obj.ssl_key_path      = privkey
    obj.ssl_issued_at     = datetime.utcnow()
    obj.ssl_expires_at    = expires_at
    obj.status            = DomainStatus.ACTIVE
    obj.nginx_config_path = config_path
    db.commit()

    _log(db, domain, "ssl_issued", f"cert={fullchain} expires={expires_at}")
    return MessageResponse(
        message=(
            "SSL certificate issued and nginx configured. "
            "Domain is live with Cloudflare protection!"
        ),
        detail=(
            f"Certificate expires: {expires_at}. "
            f"Traffic is proxied to: {obj.backend_url}. "
            f"Only Cloudflare IPs are allowed to connect."
        ),
    )


@app.put("/domains/{domain}/backend", response_model=MessageResponse, tags=["Domains"])
def update_backend(
    domain: str,
    payload: DomainCreate,
    customer: Customer = Depends(require_api_key),
    db: Session = Depends(get_db),
):
    """
    Update the backend_url for an active domain.
    Immediately rewrites the nginx config and reloads.
    """
    obj = _get_owned_domain(domain, customer, db)
    if obj.status != DomainStatus.ACTIVE:
        raise HTTPException(status_code=400, detail="Domain must be active to update backend")

    old_backend = obj.backend_url
    obj.backend_url = payload.backend_url
    db.commit()

    if obj.ssl_cert_path and obj.ssl_key_path:
        ok, _, msg = nginx_mgr.safe_write_and_reload(
            domain=domain,
            backend_url=payload.backend_url,
            ssl_cert=obj.ssl_cert_path,
            ssl_key=obj.ssl_key_path,
        )
        if not ok:
            obj.backend_url = old_backend
            db.commit()
            raise HTTPException(status_code=500, detail=f"Nginx reload failed: {msg}")

    _log(db, domain, "backend_updated", f"{old_backend} → {payload.backend_url}")
    return MessageResponse(
        message="Backend URL updated and nginx reloaded",
        detail=f"Now proxying to: {payload.backend_url}",
    )


@app.delete("/domains/{domain}", status_code=204, tags=["Domains"])
def delete_domain(
    domain: str,
    customer: Customer = Depends(require_api_key),
    db: Session = Depends(get_db),
):
    obj = _get_owned_domain(domain, customer, db)
    nginx_mgr.remove_domain_config(domain)
    nginx_mgr.reload()
    if obj.ssl_status == SSLStatus.ACTIVE:
        try:
            revoke_and_delete_certificate(domain)
        except Exception as exc:
            logger.warning("Could not revoke cert for %s: %s", domain, exc)
    db.delete(obj)
    db.commit()


# ── Nginx ─────────────────────────────────────────────────────────────────────

@app.get("/nginx/status", response_model=NginxStatusResponse, tags=["Nginx"])
def nginx_status():
    ok, msg = nginx_mgr.test_config()
    return NginxStatusResponse(
        nginx_ok=ok,
        active_domains=len(nginx_mgr.list_active_domains()),
        detail=msg,
    )


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
    """Return info about the cached Cloudflare IP ranges."""
    return cf_mgr.get_status()


@app.post("/cloudflare/refresh", tags=["Cloudflare"])
def cloudflare_refresh(
    background_tasks: BackgroundTasks,
    update_ufw: bool = True,
):
    """
    Fetch the latest Cloudflare IP ranges and:
      - Rewrite /etc/nginx/snippets/cloudflare-realip.conf
      - Rewrite /etc/nginx/snippets/cloudflare-allow.conf
      - Reload nginx
      - Update UFW rules (if update_ufw=true)

    Run this daily via cron (update_cloudflare_ips.sh does this automatically).
    """
    result = cf_mgr.full_refresh(update_ufw=update_ufw)
    # Reload nginx to pick up new snippets
    ok, msg = nginx_mgr.reload()
    result["nginx_reloaded"] = ok
    result["nginx_message"]  = msg
    return result


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
