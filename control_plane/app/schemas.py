"""Pydantic schemas for the Translation Proxy API."""

from pydantic import BaseModel, EmailStr, field_validator
from typing import Optional, List
from datetime import datetime
from uuid import UUID
from .models import SSLStatus, DomainStatus, CrawlFrequency, CrawlStatus, RoutingMode, UsageEventType


# ── Customer ──────────────────────────────────────────────────────────────────

class CustomerCreate(BaseModel):
    email: EmailStr


class CustomerResponse(BaseModel):
    id:             UUID
    email:          str
    api_key_prefix: str       # e.g. "sk-X7fJ2mKp…" — prefix only, never the hash
    is_active:      bool
    plan:           str
    created_at:     datetime

    model_config = {"from_attributes": True}


class CustomerCreatedResponse(CustomerResponse):
    """Returned only at creation — includes the raw API key (shown once)."""
    raw_api_key: str


# ── Domain ────────────────────────────────────────────────────────────────────

class DomainCreate(BaseModel):
    domain:       str
    backend_url:  str
    routing_mode: RoutingMode = RoutingMode.SUBDIRECTORY

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


class DomainResponse(BaseModel):
    id:                UUID
    customer_id:       UUID
    domain:            str
    backend_url:       str
    verification_token:str
    is_verified:       bool
    verified_at:       Optional[datetime]
    status:            DomainStatus
    ssl_status:        SSLStatus
    ssl_cert_path:     Optional[str]
    ssl_expires_at:    Optional[datetime]
    routing_mode:      RoutingMode
    created_at:        datetime

    model_config = {"from_attributes": True}


class DomainVerificationInstructions(BaseModel):
    domain:           str
    txt_record_name:  str
    txt_record_value: str
    a_record_value:   str
    instructions:     str


class NginxStatusResponse(BaseModel):
    nginx_ok:       bool
    active_domains: int
    detail:         str


class MessageResponse(BaseModel):
    message: str
    detail:  Optional[str] = None


# ── Translation Config ────────────────────────────────────────────────────────

class TranslationConfigCreate(BaseModel):
    languages:    str             = "DE"
    frequency:    CrawlFrequency  = CrawlFrequency.DAILY
    extra_urls:   Optional[str]   = None
    routing_mode: RoutingMode     = RoutingMode.SUBDIRECTORY

    @field_validator("languages")
    @classmethod
    def validate_languages(cls, v: str) -> str:
        langs = [l.strip().upper() for l in v.split(",") if l.strip()]
        if not langs:
            raise ValueError("At least one language required")
        return ",".join(langs)


class TranslationConfigResponse(BaseModel):
    id:           UUID
    domain_id:    UUID
    languages:    str
    frequency:    CrawlFrequency
    extra_urls:   Optional[str]
    last_crawl:   Optional[datetime]
    next_crawl:   Optional[datetime]
    routing_mode: RoutingMode
    created_at:   datetime
    updated_at:   datetime

    model_config = {"from_attributes": True}


class TranslatedPageSummary(BaseModel):
    id:            UUID
    url:           str
    language:      str
    status:        CrawlStatus
    error:         Optional[str]
    crawled_at:    Optional[datetime]
    origin_status: Optional[str]
    word_count:    int
    html_url:      Optional[str]    # CDN URL if stored in S3

    model_config = {"from_attributes": True}


class CrawlSummaryResponse(BaseModel):
    domain:            str
    urls:              int
    ok:                int
    failed:            int
    langs:             List[str]
    words_translated:  int
    message:           str


# ── Glossary ──────────────────────────────────────────────────────────────────

class GlossaryRuleCreate(BaseModel):
    source_term:    str
    language:       Optional[str] = None   # None = all languages
    replacement:    Optional[str] = None   # None = do_not_translate
    case_sensitive: bool          = False

    @field_validator("source_term")
    @classmethod
    def clean_term(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("source_term cannot be empty")
        return v

    @field_validator("language")
    @classmethod
    def clean_lang(cls, v: Optional[str]) -> Optional[str]:
        return v.upper().strip() if v else None


class GlossaryRuleResponse(BaseModel):
    id:             UUID
    domain_id:      UUID
    source_term:    str
    language:       Optional[str]
    replacement:    Optional[str]
    case_sensitive: bool
    created_at:     datetime

    model_config = {"from_attributes": True}


# ── Usage ─────────────────────────────────────────────────────────────────────

class UsageSummaryResponse(BaseModel):
    year:                  int
    month:                 int
    words_translated:      int
    pages_served:          int
    crawls_run:            int
    plan:                  str
    word_limit:            int
    word_limit_unlimited:  bool
    word_percent_used:     float
    over_limit:            bool


# ── Translation Memory ────────────────────────────────────────────────────────

class TranslationMemoryStats(BaseModel):
    total_entries:      int
    total_cache_hits:   int
    hot_cache_size:     int
    by_language:        dict
