import uuid
from datetime import datetime
from sqlalchemy import (
    Column, String, Boolean, DateTime, ForeignKey, Text, Enum
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
import enum

from .database import Base


class SSLStatus(str, enum.Enum):
    PENDING   = "pending"
    ACTIVE    = "active"
    FAILED    = "failed"
    EXPIRED   = "expired"
    RENEWING  = "renewing"


class DomainStatus(str, enum.Enum):
    PENDING_VERIFICATION = "pending_verification"
    VERIFIED             = "verified"
    ACTIVE               = "active"
    SUSPENDED            = "suspended"
    FAILED               = "failed"


class Customer(Base):
    __tablename__ = "customers"

    id         = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email      = Column(String(255), unique=True, nullable=False, index=True)
    api_key    = Column(String(64),  unique=True, nullable=False, index=True)
    is_active  = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    domains = relationship("Domain", back_populates="customer",
                           cascade="all, delete-orphan")


class Domain(Base):
    __tablename__ = "domains"

    id                 = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    customer_id        = Column(UUID(as_uuid=True), ForeignKey("customers.id"), nullable=False)
    domain             = Column(String(253), unique=True, nullable=False, index=True)
    # Where we proxy traffic TO for this customer
    backend_url        = Column(String(2048), nullable=False)

    # DNS ownership verification
    verification_token = Column(String(64),  nullable=False)
    is_verified        = Column(Boolean, default=False)
    verified_at        = Column(DateTime, nullable=True)

    # Domain lifecycle
    status             = Column(Enum(DomainStatus), default=DomainStatus.PENDING_VERIFICATION)

    # SSL certificate state
    ssl_status         = Column(Enum(SSLStatus), default=SSLStatus.PENDING)
    ssl_cert_path      = Column(String(512), nullable=True)
    ssl_key_path       = Column(String(512), nullable=True)
    ssl_issued_at      = Column(DateTime, nullable=True)
    ssl_expires_at     = Column(DateTime, nullable=True)

    # Nginx config
    nginx_config_path  = Column(String(512), nullable=True)

    # Audit
    created_at         = Column(DateTime, default=datetime.utcnow)
    updated_at         = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    customer = relationship("Customer", back_populates="domains")


class ProxyRequestLog(Base):
    """Lightweight audit log for proxy request errors (not normal traffic – nginx handles that)."""
    __tablename__ = "proxy_request_logs"

    id         = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    domain     = Column(String(253), nullable=False, index=True)
    event      = Column(String(64))      # e.g. ssl_issued, ssl_failed, domain_verified
    detail     = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
