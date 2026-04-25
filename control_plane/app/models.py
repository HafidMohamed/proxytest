"""
Database models for the Translation Proxy platform.
"""
import uuid
from datetime import datetime, timezone
from sqlalchemy import (
    Column, String, Boolean, DateTime, ForeignKey, Text, Enum,
    Integer, BigInteger, Index, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
import enum

from .database import Base


# ─────────────────────────────────────────────────────────────────────────────
# Enums
# ─────────────────────────────────────────────────────────────────────────────

class SSLStatus(str, enum.Enum):
    PENDING  = "pending"
    ACTIVE   = "active"
    FAILED   = "failed"
    EXPIRED  = "expired"
    RENEWING = "renewing"


class DomainStatus(str, enum.Enum):
    PENDING_VERIFICATION = "pending_verification"
    VERIFIED             = "verified"
    ACTIVE               = "active"
    SUSPENDED            = "suspended"
    FAILED               = "failed"


class CrawlFrequency(str, enum.Enum):
    HOURLY = "hourly"
    DAILY  = "daily"
    WEEKLY = "weekly"
    MANUAL = "manual"


class CrawlStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE    = "done"
    FAILED  = "failed"


class RoutingMode(str, enum.Enum):
    SUBDIRECTORY = "subdirectory"   # example.com/de/
    SUBDOMAIN    = "subdomain"      # de.example.com


class UsageEventType(str, enum.Enum):
    WORDS_TRANSLATED = "words_translated"
    PAGE_SERVED      = "page_served"
    CRAWL_RUN        = "crawl_run"


# ─────────────────────────────────────────────────────────────────────────────
# Core models
# ─────────────────────────────────────────────────────────────────────────────

class Customer(Base):
    __tablename__ = "customers"

    id              = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email           = Column(String(255), unique=True, nullable=False, index=True)
    # api_key is now the hashed value; raw key returned only at creation
    api_key_hash    = Column(String(128), unique=True, nullable=False, index=True)
    # Keep api_key_prefix (first 8 chars) for user display ("sk-aBcD1234…")
    api_key_prefix  = Column(String(12), nullable=False)
    is_active       = Column(Boolean, default=True)
    # Plan: free | starter | pro | enterprise
    plan            = Column(String(32), default="free")
    # Billing limits
    monthly_word_limit   = Column(Integer, default=2000)
    monthly_page_limit   = Column(Integer, default=0)   # 0 = unlimited
    created_at      = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    domains      = relationship("Domain", back_populates="customer", cascade="all, delete-orphan")
    usage_events = relationship("UsageEvent", back_populates="customer", cascade="all, delete-orphan")


class Domain(Base):
    __tablename__ = "domains"

    id                 = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    customer_id        = Column(UUID(as_uuid=True), ForeignKey("customers.id"), nullable=False)
    domain             = Column(String(253), unique=True, nullable=False, index=True)
    backend_url        = Column(String(2048), nullable=False)

    # Routing mode: subdirectory (/de/) or subdomain (de.example.com)
    routing_mode       = Column(Enum(RoutingMode), default=RoutingMode.SUBDIRECTORY)

    # DNS ownership verification
    verification_token = Column(String(64), nullable=False)
    is_verified        = Column(Boolean, default=False)
    verified_at        = Column(DateTime, nullable=True)

    status             = Column(Enum(DomainStatus), default=DomainStatus.PENDING_VERIFICATION)
    ssl_status         = Column(Enum(SSLStatus), default=SSLStatus.PENDING)
    ssl_cert_path      = Column(String(512), nullable=True)
    ssl_key_path       = Column(String(512), nullable=True)
    ssl_issued_at      = Column(DateTime, nullable=True)
    ssl_expires_at     = Column(DateTime, nullable=True)
    nginx_config_path  = Column(String(512), nullable=True)

    created_at         = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at         = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                                onupdate=lambda: datetime.now(timezone.utc))

    customer           = relationship("Customer", back_populates="domains")


class ProxyRequestLog(Base):
    """Lightweight audit log for proxy events."""
    __tablename__ = "proxy_request_logs"

    id         = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    domain     = Column(String(253), nullable=False, index=True)
    event      = Column(String(64))
    detail     = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


# ─────────────────────────────────────────────────────────────────────────────
# Translation / SEO models
# ─────────────────────────────────────────────────────────────────────────────

class TranslationConfig(Base):
    """Per-domain translation settings."""
    __tablename__ = "translation_configs"

    id         = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    domain_id  = Column(UUID(as_uuid=True), ForeignKey("domains.id", ondelete="CASCADE"),
                        unique=True, nullable=False)
    # Comma-separated DeepL language codes: "DE,FR,ES"
    languages  = Column(String(256), nullable=False, default="DE")
    frequency  = Column(Enum(CrawlFrequency), default=CrawlFrequency.DAILY)
    extra_urls = Column(Text, nullable=True)
    last_crawl = Column(DateTime, nullable=True)
    next_crawl = Column(DateTime, nullable=True)

    # Routing mode mirrors Domain.routing_mode but can be overridden
    routing_mode = Column(Enum(RoutingMode), default=RoutingMode.SUBDIRECTORY)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    domain = relationship("Domain", backref="translation_config", uselist=False)
    pages  = relationship("TranslatedPage", back_populates="config",
                          cascade="all, delete-orphan")


class TranslatedPage(Base):
    """
    One cached translated page per (config, url, language).
    html_url points to S3/CDN when CDN storage is enabled;
    html column holds the content when CDN is disabled (dev mode).
    """
    __tablename__ = "translated_pages"

    id            = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    config_id     = Column(UUID(as_uuid=True), ForeignKey("translation_configs.id",
                            ondelete="CASCADE"), nullable=False)
    url           = Column(String(2048), nullable=False)
    language      = Column(String(10), nullable=False)
    # One of these will be set:
    html          = Column(Text, nullable=True)      # inline storage (dev/small)
    html_url      = Column(String(2048), nullable=True)  # S3/CDN URL (production)
    status        = Column(Enum(CrawlStatus), default=CrawlStatus.PENDING)
    error         = Column(Text, nullable=True)
    crawled_at    = Column(DateTime, nullable=True)
    origin_status = Column(String(8), nullable=True)
    word_count    = Column(Integer, default=0)      # words translated for billing

    created_at    = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at    = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc))

    config = relationship("TranslationConfig", back_populates="pages")

    __table_args__ = (
        UniqueConstraint("config_id", "url", "language", name="uq_page_url_lang"),
        Index("ix_translated_pages_config_lang", "config_id", "language"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# NEW: Translation Memory
# ─────────────────────────────────────────────────────────────────────────────

class TranslationMemory(Base):
    """
    Stores (source_text, language) → translated_text mappings.
    Shared across all customers. Keyed by SHA-256 of source text
    to enable fast O(1) lookups without full-text search.
    """
    __tablename__ = "translation_memory"

    id             = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # SHA-256 hex of source_text (64 chars) — fast indexed lookup
    source_hash    = Column(String(64), nullable=False, index=True)
    source_text    = Column(Text, nullable=False)
    language       = Column(String(10), nullable=False)
    translated_text = Column(Text, nullable=False)
    # How many times this entry has been reused (for analytics)
    hit_count      = Column(Integer, default=0)
    created_at     = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at     = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                            onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("source_hash", "language", name="uq_tm_hash_lang"),
        Index("ix_tm_hash_lang", "source_hash", "language"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# NEW: Glossary Rules
# ─────────────────────────────────────────────────────────────────────────────

class GlossaryRule(Base):
    """
    Per-domain glossary: terms that must not be translated or must
    always be replaced with a specific string.

    Modes:
      - replacement: always substitute source_term → replacement (per language)
      - do_not_translate: mark term for DeepL's ignore list
    """
    __tablename__ = "glossary_rules"

    id            = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    domain_id     = Column(UUID(as_uuid=True), ForeignKey("domains.id", ondelete="CASCADE"),
                           nullable=False)
    source_term   = Column(String(512), nullable=False)
    # NULL language means applies to ALL languages for this domain
    language      = Column(String(10), nullable=True)
    replacement   = Column(String(512), nullable=True)   # NULL = do_not_translate
    case_sensitive = Column(Boolean, default=False)
    created_at    = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    domain = relationship("Domain", backref="glossary_rules")

    __table_args__ = (
        Index("ix_glossary_domain_lang", "domain_id", "language"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# NEW: Usage / Billing Events
# ─────────────────────────────────────────────────────────────────────────────

class UsageEvent(Base):
    """
    Append-only ledger for metered billing.
    Each row records one countable event (words translated, pages served, etc.)
    Roll up monthly with: SELECT sum(quantity) WHERE customer_id=X AND month=Y
    """
    __tablename__ = "usage_events"

    id          = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    customer_id = Column(UUID(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"),
                         nullable=False)
    domain_id   = Column(UUID(as_uuid=True), ForeignKey("domains.id", ondelete="SET NULL"),
                         nullable=True)
    event_type  = Column(Enum(UsageEventType), nullable=False)
    quantity    = Column(BigInteger, nullable=False, default=0)
    language    = Column(String(10), nullable=True)
    url         = Column(String(2048), nullable=True)
    occurred_at = Column(DateTime, nullable=False,
                         default=lambda: datetime.now(timezone.utc))

    customer = relationship("Customer", back_populates="usage_events")

    __table_args__ = (
        Index("ix_usage_customer_month", "customer_id", "occurred_at"),
    )
