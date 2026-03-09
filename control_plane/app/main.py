"""
Translation Proxy – Control Plane API
======================================

Endpoints
---------
POST   /customers                      Create a customer account
GET    /customers/{customer_id}        Get customer details

POST   /domains                        Register a new domain (returns verification instructions)
GET    /domains/{domain}               Get domain status
POST   /domains/{domain}/verify        Trigger DNS verification check
POST   /domains/{domain}/provision-ssl Trigger SSL certificate issuance
DELETE /domains/{domain}               Remove a domain and its nginx/SSL config

GET    /nginx/status                   Check nginx health
POST   /nginx/reload                   Force nginx reload
POST   /ssl/renew-all                  Trigger certbot renew for all certs

GET    /health                         Liveness check
"""

import logging
import secrets
import uuid
from datetime import datetime, timezone
from typing import List

from fastapi import FastAPI, HTTPException, Depends, BackgroundTasks, Header, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from .config import settings
from .database import engine, Base, get_db
from .models import Customer, Domain, DomainStatus, SSLStatus, ProxyRequestLog
from .schemas import (
    CustomerCreate, CustomerResponse,
    DomainCreate, DomainResponse, DomainVerificationInstructions,
    MessageResponse, NginxStatusResponse,
)
from .services.nginx_manager import NginxManager
from .services import (
    full_domain_check,
    issue_certificate, cert_exists, cert_paths, get_cert_expiry,
    revoke_and_delete_certificate, renew_all_certificates,
)

# Create tables on startup (use Alembic in production for migrations)
Base.metadata.create_all(bind=engine)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Translation Proxy – Control Plane",
    description="Manages customer domains, DNS verification, SSL certificates and Nginx configs.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

nginx_mgr = NginxManager()


# ── Auth helper ───────────────────────────────────────────────────────────────

def _get_customer_by_api_key(db: Session, api_key: str) -> Customer:
    customer = db.query(Customer).filter(Customer.api_key == api_key).first()
    if not customer or not customer.is_active:
        raise HTTPException(status_code=401, detail="Invalid or inactive API key")
    return customer


def require_api_key(
    x_api_key: str = Header(..., alias="X-API-Key"),
    db: Session = Depends(get_db),
) -> Customer:
    return _get_customer_by_api_key(db, x_api_key)


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health", tags=["System"])
def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


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
    logger.info("Customer created: %s", customer.email)
    return customer


@app.get("/customers/{customer_id}", response_model=CustomerResponse, tags=["Customers"])
def get_customer(customer_id: str, customer: Customer = Depends(require_api_key)):
    if str(customer.id) != customer_id:
        raise HTTPException(status_code=403, detail="Forbidden")
    return customer


# ── Domains ───────────────────────────────────────────────────────────────────

@app.post("/domains", response_model=DomainVerificationInstructions, status_code=201, tags=["Domains"])
def register_domain(
    payload: DomainCreate,
    customer: Customer = Depends(require_api_key),
    db: Session = Depends(get_db),
):
    """
    Register a new domain.  Returns the DNS records the customer must create:
      1. TXT  _proxy-verify.<domain>  "proxy-verify=<token>"
      2. A    <domain>                <our_server_ip>
    """
    existing = db.query(Domain).filter(Domain.domain == payload.domain).first()
    if existing:
        if str(existing.customer_id) != str(customer.id):
            raise HTTPException(status_code=409, detail="Domain already registered to another customer")
        # Return instructions again (idempotent)
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

    # Write HTTP-only nginx config immediately so ACME challenges can be served
    # as soon as the customer points their DNS to us.
    try:
        nginx_mgr.write_http_only_config(payload.domain)
        nginx_mgr.reload()
    except Exception as exc:
        logger.warning("Could not write http-only config for %s: %s", payload.domain, exc)

    logger.info("Domain registered: %s for customer %s", payload.domain, customer.email)
    return _build_instructions(domain_obj)


def _build_instructions(domain_obj: Domain) -> DomainVerificationInstructions:
    txt_name  = f"{settings.VERIFICATION_TXT_PREFIX}.{domain_obj.domain}"
    txt_value = f"proxy-verify={domain_obj.verification_token}"
    return DomainVerificationInstructions(
        domain=domain_obj.domain,
        txt_record_name=txt_name,
        txt_record_value=txt_value,
        a_record_value=settings.OUR_SERVER_IP,
        instructions=(
            f"Add these two DNS records at your domain registrar:\n\n"
            f"  TYPE  NAME                          VALUE\n"
            f"  TXT   {txt_name:<32}  \"{txt_value}\"\n"
            f"  A     {domain_obj.domain:<32}  {settings.OUR_SERVER_IP}\n\n"
            f"DNS propagation can take up to 24 hours. "
            f"Once done, call POST /domains/{domain_obj.domain}/verify"
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
    obj = db.query(Domain).filter(Domain.domain == domain).first()
    if not obj:
        raise HTTPException(status_code=404, detail="Domain not found")
    if str(obj.customer_id) != str(customer.id):
        raise HTTPException(status_code=403, detail="Forbidden")
    return obj


@app.post("/domains/{domain}/verify", response_model=MessageResponse, tags=["Domains"])
def verify_domain(
    domain: str,
    customer: Customer = Depends(require_api_key),
    db: Session = Depends(get_db),
    background_tasks: BackgroundTasks = BackgroundTasks(),
):
    """
    Check that the customer has added the TXT record and A record.
    On success, marks the domain as verified.
    """
    obj = _get_owned_domain(domain, customer, db)

    if obj.is_verified:
        return MessageResponse(message="Domain already verified", detail=str(obj.verified_at))

    all_ok, details = full_domain_check(domain, obj.verification_token)

    _log(db, domain, "dns_check", str(details))

    if not all_ok:
        return MessageResponse(
            message="DNS verification failed",
            detail=str(details),
        )

    obj.is_verified   = True
    obj.verified_at   = datetime.utcnow()
    obj.status        = DomainStatus.VERIFIED
    db.commit()

    logger.info("Domain verified: %s", domain)
    return MessageResponse(
        message="Domain verified successfully. Now call /provision-ssl to issue your certificate.",
        detail=str(details),
    )


@app.post("/domains/{domain}/provision-ssl", response_model=MessageResponse, tags=["Domains"])
def provision_ssl(
    domain: str,
    customer: Customer = Depends(require_api_key),
    db: Session = Depends(get_db),
    background_tasks: BackgroundTasks = BackgroundTasks(),
):
    """
    Issue a Let's Encrypt certificate and activate the full HTTPS nginx config.
    Domain must be verified first.
    """
    obj = _get_owned_domain(domain, customer, db)

    if not obj.is_verified:
        raise HTTPException(status_code=400, detail="Domain must be verified before SSL can be provisioned")

    if obj.ssl_status == SSLStatus.ACTIVE and cert_exists(domain):
        return MessageResponse(message="SSL certificate already active")

    obj.ssl_status = SSLStatus.PENDING
    db.commit()

    # Run synchronously (can be moved to a Celery/RQ task for production)
    success, msg = issue_certificate(domain)

    if not success:
        obj.ssl_status = SSLStatus.FAILED
        db.commit()
        _log(db, domain, "ssl_failed", msg)
        raise HTTPException(status_code=500, detail=f"SSL issuance failed: {msg}")

    # Cert issued – update DB and write nginx SSL config
    fullchain, privkey = cert_paths(domain)
    expires_at = get_cert_expiry(domain)

    obj.ssl_status      = SSLStatus.ACTIVE
    obj.ssl_cert_path   = fullchain
    obj.ssl_key_path    = privkey
    obj.ssl_issued_at   = datetime.utcnow()
    obj.ssl_expires_at  = expires_at
    obj.status          = DomainStatus.ACTIVE

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

    obj.nginx_config_path = config_path
    db.commit()

    _log(db, domain, "ssl_issued", f"cert={fullchain} expires={expires_at}")
    logger.info("SSL provisioned and nginx reloaded for %s", domain)
    return MessageResponse(
        message="SSL certificate issued and nginx configured. Domain is now live!",
        detail=f"Certificate expires: {expires_at}",
    )


@app.delete("/domains/{domain}", status_code=204, tags=["Domains"])
def delete_domain(
    domain: str,
    customer: Customer = Depends(require_api_key),
    db: Session = Depends(get_db),
):
    obj = _get_owned_domain(domain, customer, db)

    # Remove nginx config
    nginx_mgr.remove_domain_config(domain)
    nginx_mgr.reload()

    # Optionally revoke cert
    if obj.ssl_status == SSLStatus.ACTIVE:
        try:
            revoke_and_delete_certificate(domain)
        except Exception as exc:
            logger.warning("Could not revoke cert for %s: %s", domain, exc)

    db.delete(obj)
    db.commit()
    logger.info("Domain deleted: %s", domain)


# ── Nginx ─────────────────────────────────────────────────────────────────────

@app.get("/nginx/status", response_model=NginxStatusResponse, tags=["Nginx"])
def nginx_status():
    running = nginx_mgr.is_nginx_running()
    ok, msg = nginx_mgr.test_config()
    return NginxStatusResponse(
        nginx_ok=running and ok,
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
