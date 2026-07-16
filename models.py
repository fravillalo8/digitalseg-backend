from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, field_validator, Field
import re


class Requirements(BaseModel):
    space: str = Field(max_length=80)
    doorType: str = Field(max_length=80)
    thickness: Optional[int] = None
    features: list[str] = Field(default=[], max_length=20)
    budgetMin: Optional[int] = None
    budgetMax: Optional[int] = None
    instalacion: Optional[str] = Field(default=None, max_length=20)   # 'madera' | 'reja'


class Recommendation(BaseModel):
    id: str = Field(max_length=120)
    brand: str = Field(max_length=120)
    name: str = Field(max_length=200)
    sku: str = Field(max_length=200)
    price: float
    needsGateway: bool = False
    total: float


class Customer(BaseModel):
    nombre: str = Field(max_length=120)
    ciudad: Optional[str] = Field(default=None, max_length=80)
    telefono: str = Field(max_length=30)
    email: Optional[str] = Field(default=None, max_length=120)
    cantidad: str = Field(default="1", max_length=10)
    cotizacionFormal: bool = False
    razonSocial: Optional[str] = Field(default=None, max_length=150)
    rut: Optional[str] = Field(default=None, max_length=20)

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
    slug: Optional[str] = None            # slug de la cotización de valor publicada
    cotizacion_url: Optional[str] = None  # https://cotizacion.digitalseg.cl/q/?c=<slug>
    message: str


class SendTemplateRequest(BaseModel):
    to: str
    template: str
    params: list[str] = []


# ── Agenda ────────────────────────────────────────────────────────────────────

class VisitaRequest(BaseModel):
    nombre: str = Field(max_length=120)
    telefono: str = Field(max_length=30)
    direccion: Optional[str] = Field(default=None, max_length=200)
    proyecto: str = Field(default="Hogar", max_length=80)
    fecha: str = Field(max_length=10)   # "2026-05-12"
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
    cliente: str = Field(max_length=120)
    telefono: str = Field(max_length=30)
    producto: str = Field(max_length=200)
    sku: Optional[str] = Field(default=None, max_length=500)
    precio: Optional[int] = None  # ignorado — el backend calcula desde el catálogo
    cantidad: int = 1
    gateway: bool = False
    lead_id: Optional[int] = None
    ref: Optional[str] = Field(default=None, max_length=80)
    ref_embajador: Optional[str] = Field(default=None, max_length=40)  # código de embajador (?ref= en el enlace)
    cupon: Optional[str] = Field(default=None, max_length=40)
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
    nombre: str = Field(max_length=120)
    telefono: str = Field(max_length=30)
    email: Optional[str] = Field(default=None, max_length=120)
    score: int
    nivel: str = Field(max_length=30)
    zona: Optional[str] = Field(default=None, max_length=80)
    tipo_propiedad: Optional[str] = Field(default=None, max_length=80)
    respuestas: dict = {}
    findings: list[str] = Field(default=[], max_length=50)
    recs: list[str] = Field(default=[], max_length=50)


# ── Cotización de un VENDEDOR (programa "Gana con Digitalseg") ────────────────
# El portal del vendedor (gana.digitalseg.cl) manda esto al backend para publicar
# la página de valor /q/ y enviar el correo al cliente + aviso al equipo.
class VendorCotItem(BaseModel):
    name: str = Field(max_length=160)
    qty: int = 1
    price: int = 0
    kind: str = Field(default="product", max_length=20)   # 'product' | 'install'
    img: Optional[str] = Field(default=None, max_length=400)   # URL absoluta (para el correo)
    feats: list[str] = Field(default=[], max_length=12)


class VendorCotPayload(BaseModel):
    numero: str = Field(default="", max_length=40)
    vendedor_nombre: str = Field(default="", max_length=120)
    vendedor_codigo: str = Field(default="", max_length=40)
    vendedor_email: Optional[str] = Field(default=None, max_length=140)
    cliente: str = Field(default="", max_length=140)
    cliente_email: Optional[str] = Field(default=None, max_length=140)
    cliente_telefono: Optional[str] = Field(default=None, max_length=40)
    items: list[VendorCotItem] = Field(default=[], max_length=40)
    descuento_pct: float = 0
    descuento: int = 0
    monto: int = 0          # lo que paga el cliente (con descuento)
    monto_lista: int = 0    # precio de lista


class VendorCotResponse(BaseModel):
    ok: bool
    published: bool = False
    client_emailed: bool = False
    team_notified: bool = False
    page_url: str = ""
    folio: str = ""


# ── Relevamiento de puerta (el vendedor manda fotos + medidas; la IA recomienda) ──
class RelevFoto(BaseModel):
    tipo: str = Field(default="", max_length=40)   # completa|canto|afuera|adentro|marco
    b64: str = ""                                  # JPEG en base64 (ya comprimido en el navegador)


class RelevamientoPayload(BaseModel):
    relevamiento_id: Optional[str] = Field(default=None, max_length=60)
    cliente: str = Field(default="", max_length=140)
    medidas: dict = {}                             # {espesor_mm, backset_mm, material, sentido, ...}
    fotos: list[RelevFoto] = Field(default=[], max_length=6)
    catalogo: list[dict] = Field(default=[], max_length=40)
