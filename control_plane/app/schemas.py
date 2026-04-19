from pydantic import BaseModel, EmailStr, HttpUrl, field_validator
from typing import Optional
from datetime import datetime
from uuid import UUID
from .models import SSLStatus, DomainStatus


# ── Customer ──────────────────────────────────────────────────────────────────

class CustomerCreate(BaseModel):
    email: EmailStr


class CustomerResponse(BaseModel):
    id: UUID
    email: str
    api_key: str
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Domain ────────────────────────────────────────────────────────────────────

class DomainCreate(BaseModel):
    domain: str
    backend_url: str

    @field_validator("domain")
    @classmethod
    def clean_domain(cls, v: str) -> str:
        return v.lower().strip().rstrip("/")

    @field_validator("backend_url")
    @classmethod
    def clean_backend(cls, v: str) -> str:
        v = v.strip().rstrip("/")
        if not v.startswith(("http://", "https://")):
            raise ValueError("backend_url must start with http:// or https://")
        return v


class DomainVerifyRequest(BaseModel):
    domain: str


class DomainResponse(BaseModel):
    id: UUID
    customer_id: UUID
    domain: str
    backend_url: str
    verification_token: str
    is_verified: bool
    verified_at: Optional[datetime]
    status: DomainStatus
    ssl_status: SSLStatus
    ssl_cert_path: Optional[str]
    ssl_expires_at: Optional[datetime]
    created_at: datetime

    model_config = {"from_attributes": True}


class DomainVerificationInstructions(BaseModel):
    domain: str
    txt_record_name: str     # e.g. _proxy-verify.example.com
    txt_record_value: str    # the token
    a_record_value: str      # our server IP
    instructions: str


class SSLProvisionRequest(BaseModel):
    domain: str


class NginxStatusResponse(BaseModel):
    nginx_ok: bool
    active_domains: int
    detail: str


# ── Generic ───────────────────────────────────────────────────────────────────

class MessageResponse(BaseModel):
    message: str
    detail: Optional[str] = None


# ── Translation / SEO ─────────────────────────────────────────────────────────

from typing import List
from .models import CrawlFrequency, CrawlStatus


class TranslationConfigCreate(BaseModel):
    """
    Set up translation for a domain.
    languages: comma-separated DeepL language codes, e.g. "DE,FR,ES"
    frequency: hourly | daily | weekly | manual
    extra_urls: optional newline-separated URLs to always crawl (in addition to sitemap)
    """
    languages:  str           = "DE"
    frequency:  CrawlFrequency = CrawlFrequency.DAILY
    extra_urls: Optional[str] = None

    @field_validator("languages")
    @classmethod
    def validate_languages(cls, v: str) -> str:
        langs = [l.strip().upper() for l in v.split(",") if l.strip()]
        if not langs:
            raise ValueError("At least one language required")
        return ",".join(langs)


class TranslationConfigResponse(BaseModel):
    id:         UUID
    domain_id:  UUID
    languages:  str
    frequency:  CrawlFrequency
    extra_urls: Optional[str]
    last_crawl: Optional[datetime]
    next_crawl: Optional[datetime]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class TranslatedPageSummary(BaseModel):
    id:            UUID
    url:           str
    language:      str
    status:        CrawlStatus
    error:         Optional[str]
    crawled_at:    Optional[datetime]
    origin_status: Optional[str]

    model_config = {"from_attributes": True}


class CrawlSummaryResponse(BaseModel):
    domain:  str
    urls:    int
    ok:      int
    failed:  int
    langs:   List[str]
    message: str
