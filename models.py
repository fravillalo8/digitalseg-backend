from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, field_validator
import re


class Requirements(BaseModel):
    space: str
    doorType: str
    thickness: Optional[int] = None
    features: list[str] = []
    budgetMin: Optional[int] = None
    budgetMax: Optional[int] = None


class Recommendation(BaseModel):
    id: str
    brand: str
    name: str
    sku: str
    price: float
    needsGateway: bool = False
    total: float


class Customer(BaseModel):
    nombre: str
    ciudad: Optional[str] = None
    telefono: str
    email: Optional[str] = None
    cantidad: str = "1"
    cotizacionFormal: bool = False
    razonSocial: Optional[str] = None
    rut: Optional[str] = None

    @field_validator("rut", mode="before")
    @classmethod
    def validate_rut(cls, v: str | None) -> str | None:
        if not v:
            return None
        if not re.match(r'^\d{1,2}\.?\d{3}\.?\d{3}-[\dKk]$', v.strip()):
            raise ValueError("RUT inválido")
        return v.strip().upper()

    @field_validator("telefono")
    @classmethod
    def normalize_phone(cls, v: str) -> str:
        digits = re.sub(r"\D", "", v)
        if digits.startswith("56") and len(digits) == 11:
            return f"+{digits}"
        if digits.startswith("9") and len(digits) == 9:
            return f"+56{digits}"
        if digits.startswith("0") and len(digits) == 10:
            return f"+56{digits[1:]}"
        return f"+{digits}" if not v.startswith("+") else v


class LeadPayload(BaseModel):
    source: str
    name: str
    phone: str
    requirements: Requirements
    recommendation: Recommendation
    customer: Customer


class LeadResponse(BaseModel):
    ok: bool
    partner_id: Optional[int] = None
    lead_id: Optional[int] = None
    sale_order_id: Optional[int] = None
    odoo_lead_url: Optional[str] = None
    odoo_sale_url: Optional[str] = None
    message: str


class SendTemplateRequest(BaseModel):
    to: str
    template: str
    params: list[str] = []


# ── Agenda ────────────────────────────────────────────────────────────────────

class VisitaRequest(BaseModel):
    nombre: str
    telefono: str
    direccion: Optional[str] = None
    proyecto: str = "Hogar"
    fecha: str   # "2026-05-12"
    hora: int    # 10

    @field_validator("telefono")
    @classmethod
    def normalize_phone(cls, v: str) -> str:
        digits = re.sub(r"\D", "", v)
        if digits.startswith("56") and len(digits) == 11:
            return f"+{digits}"
        if digits.startswith("9") and len(digits) == 9:
            return f"+56{digits}"
        if digits.startswith("0") and len(digits) == 10:
            return f"+56{digits[1:]}"
        return f"+{digits}" if not v.startswith("+") else v


class ImplementacionRequest(BaseModel):
    cliente: str
    telefono: Optional[str] = None
    direccion: Optional[str] = None
    producto: Optional[str] = None
    fecha: str   # "2026-05-14"
    hora: int    # 9
    duracion: int = 2

    @field_validator("telefono", mode="before")
    @classmethod
    def normalize_phone_impl(cls, v: str | None) -> str | None:
        if not v:
            return None
        digits = re.sub(r"\D", "", v)
        if digits.startswith("56") and len(digits) == 11:
            return f"+{digits}"
        if digits.startswith("9") and len(digits) == 9:
            return f"+56{digits}"
        return f"+{digits}" if not v.startswith("+") else v


class AgendaEvent(BaseModel):
    id: int
    type: str
    fecha: str
    hora: int
    duracion: int
    cliente: str
    telefono: Optional[str] = None
    direccion: Optional[str] = None
    proyecto: Optional[str] = None
    producto: Optional[str] = None
    whatsapp_sent: bool = False


class AgendaBookingResponse(BaseModel):
    ok: bool
    message: str
    event: Optional[AgendaEvent] = None
    whatsapp_sent: bool = False
    odoo_event_id: Optional[int] = None


# ── Pago MercadoPago ──────────────────────────────────────────────────────────

class PagoRequest(BaseModel):
    cliente: str
    telefono: str
    producto: str
    sku: Optional[str] = None
    precio: Optional[int] = None  # ignorado — el backend calcula desde el catálogo
    cantidad: int = 1
    gateway: bool = False
    lead_id: Optional[int] = None
    ref: Optional[str] = None
    cupon: Optional[str] = None
    descuento: Optional[int] = None


class PagoResponse(BaseModel):
    ok: bool
    preference_id: str
    init_point: str
    sandbox_init_point: str
    total: int
    ref: str


# ── Informe de Seguridad ──────────────────────────────────────────────────────

class InformeSeguridad(BaseModel):
    nombre: str
    telefono: str
    email: Optional[str] = None
    score: int
    nivel: str
    zona: Optional[str] = None
    tipo_propiedad: Optional[str] = None
    respuestas: dict = {}
    findings: list[str] = []
    recs: list[str] = []
