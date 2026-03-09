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
