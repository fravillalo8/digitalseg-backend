from __future__ import annotations
import os
import json
import asyncio
import logging
import hmac
import hashlib
import smtplib
import socket
import ssl
from html import escape
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from email.header import Header
from contextlib import asynccontextmanager
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import AsyncIterator, Optional

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, HTTPException, Header, Request, Response
from fastapi.middleware.cors import CORSMiddleware

import httpx

from models import (
    LeadPayload, LeadResponse, SendTemplateRequest,
    VisitaRequest, ImplementacionRequest, AgendaEvent, AgendaBookingResponse,
    PagoRequest, PagoResponse, InformeSeguridad,
)
from odoo_client import OdooClient
from whatsapp_client import WhatsAppClient

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger(__name__)

# ── Rate limiting in-memory (por IP) ─────────────────────────────────────────
_rate_store: dict[str, list[datetime]] = defaultdict(list)
_RATE_WINDOW = timedelta(minutes=10)
_RATE_MAX    = 10


def _client_ip(request: Request) -> str:
    """IP real del cliente. Detrás del proxy de Railway, request.client.host es
    la IP del proxy (bucket global → auto-DoS); usamos el primer hop de
    X-Forwarded-For para que el rate-limit sea por cliente."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _check_rate(request: Request) -> None:
    ip  = _client_ip(request)
    now = datetime.utcnow()
    _rate_store[ip] = [t for t in _rate_store[ip] if now - t < _RATE_WINDOW]
    if len(_rate_store[ip]) >= _RATE_MAX:
        raise HTTPException(status_code=429, detail="Demasiadas solicitudes. Espera unos minutos.")
    _rate_store[ip].append(now)


# ── Auth admin (fail-closed) ──────────────────────────────────────────────────
def _require_admin(x_admin_key: Optional[str]) -> None:
    """Exige X-Admin-Key. Falla CERRADO: si AGENDA_ADMIN_KEY no está configurada
    o la clave no coincide, deniega. Nunca pasa sin autenticación."""
    admin_key = os.getenv("AGENDA_ADMIN_KEY", "")
    if not admin_key or not x_admin_key or not hmac.compare_digest(x_admin_key, admin_key):
        raise HTTPException(status_code=401, detail="No autorizado")


# ── Verificación de firma de webhook MercadoPago ──────────────────────────────
_processed_payments: set[str] = set()

def _verify_mp_signature(request: Request, data_id: str) -> bool:
    """Valida el header x-signature (ts + v1 HMAC-SHA256) de MercadoPago.
    Falla CERRADO: sin MP_WEBHOOK_SECRET o firma inválida → False."""
    secret = os.getenv("MP_WEBHOOK_SECRET", "")
    if not secret:
        return False
    parts = dict(
        p.split("=", 1) for p in request.headers.get("x-signature", "").split(",") if "=" in p
    )
    ts = parts.get("ts", "").strip()
    v1 = parts.get("v1", "").strip()
    if not ts or not v1:
        return False
    req_id   = request.headers.get("x-request-id", "")
    manifest = f"id:{data_id};request-id:{req_id};ts:{ts};"
    expected = hmac.new(secret.encode(), manifest.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, v1)

odoo: OdooClient
wa: WhatsAppClient

# ── Agenda in-memory store ───────────────────────────────────────────────────
# Se inicializa desde AGENDA_DATA_FILE si existe, o desde seed data.
# Persiste mientras el proceso corra. Para persistencia total usar un disco
# permanente en Render y apuntar AGENDA_DATA_FILE a él.

AGENDA_DATA_FILE = Path(os.getenv("AGENDA_DATA_FILE", "agenda_data.json"))
_agenda_lock: asyncio.Lock
_agenda_events: list[AgendaEvent] = []
_agenda_next_id: int = 100

AGENDA_SEED: list[dict] = [
    {"id":1,"type":"visita","fecha":"2026-05-12","hora":10,"duracion":1,
     "cliente":"María González","telefono":"+56 9 1234 5678","direccion":"Los Andes 234","proyecto":"Hogar"},
    {"id":2,"type":"visita","fecha":"2026-05-12","hora":14,"duracion":1,
     "cliente":"Carlos Pérez","telefono":"+56 9 8765 4321","direccion":"San Felipe 567","proyecto":"Oficina"},
    {"id":3,"type":"implementacion","fecha":"2026-05-14","hora":9,"duracion":3,
     "cliente":"Roberto Sánchez","direccion":"Av. Los Andes 890","producto":"Cerradura Digital + Cámara IP"},
    {"id":4,"type":"implementacion","fecha":"2026-05-15","hora":14,"duracion":4,
     "cliente":"Ana Martínez","direccion":"Calle Larga 123","producto":"Sistema de Acceso Empresarial"},
    {"id":5,"type":"visita","fecha":"2026-05-19","hora":11,"duracion":1,
     "cliente":"Luis Torres","telefono":"+56 9 5555 6666","direccion":"San Esteban 45","proyecto":"Empresa"},
    {"id":6,"type":"implementacion","fecha":"2026-05-20","hora":9,"duracion":2,
     "cliente":"Patricia Flores","direccion":"Rinconada 789","producto":"Cerradura Digital Hogar"},
]


def _load_agenda() -> None:
    global _agenda_events, _agenda_next_id
    if AGENDA_DATA_FILE.exists():
        try:
            raw = json.loads(AGENDA_DATA_FILE.read_text())
            _agenda_events = [AgendaEvent(**e) for e in raw]
            _agenda_next_id = max((e.id for e in _agenda_events), default=0) + 1
            log.info("Agenda cargada desde %s (%d eventos)", AGENDA_DATA_FILE, len(_agenda_events))
            return
        except Exception as exc:
            log.warning("No se pudo leer %s: %s — usando seed", AGENDA_DATA_FILE, exc)
    _agenda_events = [AgendaEvent(**e) for e in AGENDA_SEED]
    _agenda_next_id = max(e.id for e in _agenda_events) + 1


def _save_agenda() -> None:
    try:
        AGENDA_DATA_FILE.write_text(
            json.dumps([e.model_dump() for e in _agenda_events], ensure_ascii=False, indent=2)
        )
    except Exception as exc:
        log.error("No se pudo guardar agenda: %s", exc)


def _fecha_hora_label(fecha: str, hora: int) -> str:
    """Convierte '2026-05-12' + 10 → 'martes 12 de mayo a las 10:00'"""
    MESES = ["enero","febrero","marzo","abril","mayo","junio",
             "julio","agosto","septiembre","octubre","noviembre","diciembre"]
    DIAS  = ["lunes","martes","miércoles","jueves","viernes","sábado","domingo"]
    try:
        dt = datetime.strptime(fecha, "%Y-%m-%d")
        dia_nombre = DIAS[dt.weekday()]
        mes_nombre = MESES[dt.month - 1]
        return f"{dia_nombre} {dt.day} de {mes_nombre} a las {hora:02d}:00"
    except ValueError:
        return f"{fecha} {hora:02d}:00"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global odoo, wa, _agenda_lock
    odoo = OdooClient()
    wa   = WhatsAppClient()
    _agenda_lock = asyncio.Lock()
    _load_agenda()
    try:
        log.info("Odoo uid=%s | WhatsApp configurado=%s", odoo.uid, wa._configured)
    except Exception as e:
        log.warning("Odoo auth en startup falló (se reintentará por request): %s", e)
    yield


app = FastAPI(title="DigitalSeg Backend", lifespan=lifespan)

_cors_origins: list[str] = [
    o.strip()
    for o in os.getenv("DIGITALSEG_ALLOWED_ORIGIN", "https://digitalseg.cl").split(",")
    if o.strip()
]

_dev_mode = os.getenv("APP_ENV", "production") == "development"
if _dev_mode:
    _cors_origins += [
        "http://localhost:5500",
        "http://127.0.0.1:5500",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_origin_regex=(r"http://(localhost|127\.0\.0\.1)(:\d+)?" if _dev_mode else None),
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["Content-Type", "X-Admin-Key"],
    allow_credentials=False,
)

@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


# ── POST /api/leads ─────────────────────────────────────────────────────────────

@app.post("/api/leads", response_model=LeadResponse)
async def create_lead(payload: LeadPayload, request: Request) -> LeadResponse:
    _check_rate(request)
    c   = payload.customer
    rec = payload.recommendation
    req = payload.requirements

    phone    = c.telefono
    quantity = int(c.cantidad) if c.cantidad.isdigit() else 1

    log.info("Lead: %s | SKU: %s | formal: %s", c.nombre, rec.sku, c.cotizacionFormal)

    # 1. Contacto en Odoo
    try:
        partner_id = odoo.find_or_create_partner(
            name=c.nombre,
            phone=phone,
            city=c.ciudad,
            company_name=c.razonSocial,
            vat=c.rut,
            email=c.email,
        )
    except Exception as e:
        log.error("Error creando contacto en Odoo: %s", e)
        raise HTTPException(status_code=502, detail="No pudimos registrar tu contacto. Intenta más tarde.")

    # 2. Oportunidad CRM
    try:
        lead_id = odoo.create_lead(
            partner_id=partner_id,
            phone=phone,
            product_name=f"{rec.brand} {rec.name}",
            sku=rec.sku,
            price=rec.price,
            requirements=req.model_dump(),
            source_label=payload.source,
            quantity=quantity,
            needs_gateway=rec.needsGateway,
            email=c.email,
        )
    except Exception as e:
        log.error("Error creando oportunidad en Odoo: %s", e)
        raise HTTPException(status_code=502, detail="No pudimos registrar tu solicitud. Intenta más tarde.")

    # Instalación elegida por el cliente (obligatoria — parte de la garantía)
    _INSTALL = {
        "madera": (89990, "Instalación profesional — puerta de madera"),
        "reja":   (99990, "Instalación profesional — reja / fierro"),
    }
    install_price, install_label = _INSTALL.get((req.instalacion or "").lower(), (0, ""))

    # 3. Cotización automática si hay email (con stock check y fecha de entrega)
    sale_order_id = None
    email_sent = False
    if c.email:
        stock_qty = 0.0
        try:
            stock_qty = odoo.check_stock(rec.sku)
        except Exception as e:
            log.warning("No se pudo verificar stock de '%s': %s", rec.sku, e)

        in_stock      = stock_qty >= 1
        delivery_days = 2 if in_stock else 7   # sin stock: ~1 semana
        commit_dt     = (datetime.utcnow() + timedelta(days=delivery_days)).strftime("%Y-%m-%d 12:00:00")
        _stock_line   = (
            f"Stock disponible: {stock_qty:.0f} unidades — entrega/instalación estimada: {delivery_days} días hábiles."
            if in_stock else
            "Producto SIN STOCK por ahora — lo conseguimos a pedido. Entrega/instalación estimada en "
            "aproximadamente 1 semana (7 días hábiles). Te confirmamos la fecha exacta al coordinar."
        )
        if install_price:
            _install_line = (
                f"INSTALACIÓN INCLUIDA: {install_label} (${install_price:,.0f}). ".replace(",", ".")
                + "La instalación profesional es obligatoria, parte de la garantía de 12 meses."
            )
        else:
            _install_line = (
                "INSTALACIÓN: la instalación profesional es OBLIGATORIA (parte de la garantía de 12 meses): "
                "$89.990 en puerta de madera, $99.990 en reja/fierro. DigitalSeg no vende cerraduras sin instalación."
            )
        stock_note    = _stock_line + "\n\n" + _install_line
        log.info("Stock '%s': %.0f uds | entrega: %d días | commit: %s", rec.sku, stock_qty, delivery_days, commit_dt)

        product_id = odoo.find_product(rec.sku)
        if not product_id:
            log.warning("SKU '%s' no existe en Odoo — cotización sin producto vinculado", rec.sku)
        try:
            sale_order_id = odoo.create_sale_order(
                partner_id=partner_id,
                product_id=product_id or 1,
                product_name=f"{rec.brand} {rec.name}",
                price=rec.price,
                quantity=quantity,
                commitment_date=commit_dt,
                note=stock_note,
                install_price=install_price,
                install_label=install_label,
            )
            log.info("Sale order id=%s creada", sale_order_id)
        except Exception as e:
            log.error("Error creando cotización: %s", e)

        if sale_order_id:
            try:
                html = _build_cotizacion_html(
                    nombre=c.nombre,
                    producto=f"{rec.brand} {rec.name}",
                    sku=rec.sku,
                    price=rec.price,
                    quantity=quantity,
                    in_stock=in_stock,
                    delivery_days=delivery_days,
                    odoo_url=odoo.sale_url(sale_order_id),
                    install_price=install_price,
                    install_label=install_label,
                )
                _send_email(
                    subject=f"Tu cotización DigitalSeg — {rec.brand} {rec.name}",
                    html=html,
                    to_addresses=[c.email],
                )
                lead_html = _build_lead_html(
                    c, rec, req, quantity, install_label, install_price,
                    lead_url=odoo.lead_url(lead_id),
                    sale_url=odoo.sale_url(sale_order_id),
                    source=payload.source,
                )
                _send_email(
                    subject=f"🔔 Nuevo lead: {c.nombre} — {rec.brand} {rec.name} × {quantity}",
                    html=lead_html,
                    to_addresses=_INFORME_RECIPIENTS,
                )
                email_sent = True
                log.info("Email cotización enviado al cliente %s y resumen de lead al equipo", c.email)
            except Exception as e:
                log.error("Error enviando email de cotización: %s", e)

            # Enviar la cotización OFICIAL de Odoo (presupuesto formal) al cliente
            try:
                if odoo.send_quotation_email(sale_order_id):
                    log.info("Cotización oficial de Odoo enviada al cliente %s", c.email)
                else:
                    log.warning("No se encontró plantilla de presupuesto en Odoo — presupuesto oficial no enviado")
            except Exception as e:
                log.error("Error enviando cotización oficial de Odoo: %s", e)

    elif c.cotizacionFormal:
        product_id = odoo.find_product(rec.sku)
        if not product_id:
            log.warning("SKU '%s' no existe en Odoo — cotización sin producto vinculado", rec.sku)
        try:
            sale_order_id = odoo.create_sale_order(
                partner_id=partner_id,
                product_id=product_id or 1,
                product_name=f"{rec.brand} {rec.name}",
                price=rec.price,
                quantity=quantity,
                install_price=install_price,
                install_label=install_label,
            )
        except Exception as e:
            log.error("Error creando cotización: %s", e)

    # 4. WhatsApp: confirmar solicitud recibida al cliente
    try:
        wa.solicitud_recibida(
            to=phone,
            nombre=c.nombre,
            producto=f"{rec.brand} {rec.name}",
            puerta=req.doorType,
            ciudad=c.ciudad or "",
        )
    except Exception as e:
        log.warning("WhatsApp no enviado (no bloquea): %s", e)

    # 5. WhatsApp: notificar a ventas (Sebastián) del nuevo lead
    try:
        wa.notificar_lead_nuevo(
            nombre=c.nombre,
            telefono=phone,
            ciudad=c.ciudad or "",
            producto=f"{rec.brand} {rec.name}",
            precio=rec.price,
            odoo_url=odoo.lead_url(lead_id),
        )
    except Exception as e:
        log.warning("Notificación ventas no enviada (no bloquea): %s", e)

    return LeadResponse(
        ok=True,
        partner_id=partner_id,
        lead_id=lead_id,
        sale_order_id=sale_order_id,
        odoo_lead_url=odoo.lead_url(lead_id),
        odoo_sale_url=odoo.sale_url(sale_order_id) if sale_order_id else None,
        message="Lead creado correctamente",
    )


# ── GET /api/whatsapp/webhook — verificación Meta ───────────────────────────────

@app.get("/api/whatsapp/webhook")
async def whatsapp_verify(request: Request) -> Response:
    params       = dict(request.query_params)
    verify_token = os.getenv("META_VERIFY_TOKEN", "")
    if (
        verify_token
        and params.get("hub.mode") == "subscribe"
        and params.get("hub.verify_token") == verify_token
    ):
        return Response(content=params["hub.challenge"], media_type="text/plain")
    raise HTTPException(status_code=403, detail="Verification failed")


# ── POST /api/whatsapp/webhook — mensajes entrantes ─────────────────────────────

@app.post("/api/whatsapp/webhook")
async def whatsapp_webhook(request: Request) -> dict:
    raw  = await request.body()
    sig  = request.headers.get("X-Hub-Signature-256", "")

    if not wa.verify_signature(raw, sig):
        raise HTTPException(status_code=403, detail="Invalid signature")

    body     = await request.json() if not raw else __import__("json").loads(raw)
    messages = WhatsAppClient.parse_incoming(body)

    for msg in messages:
        log.info("WA entrante de +***%s (%s): %s…", msg["from"][-4:], msg["name"][:2] + "***", msg["text"][:40])
        _update_lead_from_whatsapp(msg)

    return {"status": "ok"}


def _update_lead_from_whatsapp(msg: dict) -> None:
    """Agrega nota al lead de Odoo y reenvía el mensaje al operador."""
    phone = f"+{msg['from']}" if not msg["from"].startswith("+") else msg["from"]

    # 1. Notificar al operador en su teléfono
    try:
        wa.notificar_operador(
            from_name=msg["name"] or phone,
            from_phone=phone,
            text=msg["text"] or f"[{msg.get('type','desconocido')}]",
        )
    except Exception as e:
        log.warning("No se pudo reenviar mensaje al operador: %s", e)

    # 2. Registrar nota en el lead de Odoo
    try:
        ids = odoo._exec(
            "crm.lead", "search",
            [[["phone", "=", phone], ["active", "=", True]]],
            {"limit": 1},
        )
        if not ids:
            log.info("Sin lead activo para %s — solo se reenvió al operador", phone)
            return

        note = (
            f"<p><b>Mensaje WhatsApp</b> de {escape(str(msg['name']))} ({escape(phone)})</p>"
            f"<p>{escape(str(msg['text']))}</p>"
        )
        odoo._exec("crm.lead", "write", [[ids[0]], {"description": note}])
        log.info("Lead id=%s actualizado con mensaje WA", ids[0])
    except Exception as e:
        log.error("Error actualizando lead desde WA: %s", e)


# ── POST /api/whatsapp/send-template ────────────────────────────────────────────

@app.post("/api/whatsapp/send-template")
async def send_template(
    req: SendTemplateRequest,
    request: Request,
    x_admin_key: Optional[str] = Header(default=None),
) -> dict:
    # Endpoint operativo (NO público): exige X-Admin-Key + rate-limit para
    # que nadie pueda enviar plantillas de WhatsApp a números arbitrarios
    # desde el WABA de la empresa (spam / costo / baneo).
    _check_rate(request)
    _require_admin(x_admin_key)
    to_clean = (req.to or "").lstrip("+")
    if not to_clean.isdigit() or not (8 <= len(to_clean) <= 15):
        raise HTTPException(status_code=422, detail="Número inválido")
    try:
        if req.template == "solicitud_cotizacion_recibida":
            result = wa.solicitud_recibida(
                to=req.to,
                nombre=req.params[0],
                producto=req.params[1],
                puerta=req.params[2],
                ciudad=req.params[3] if len(req.params) > 3 else "",
            )
        elif req.template == "visita_tecnica_agendada":
            result = wa.visita_agendada(
                to=req.to,
                nombre=req.params[0],
                fecha_hora=req.params[1],
            )
        elif req.template == "cotizacion_formal_lista":
            result = wa.cotizacion_lista(
                to=req.to,
                nombre=req.params[0],
                producto=req.params[1],
                total=req.params[2],
            )
        else:
            result = wa.send_template(to=req.to, template_name=req.template)
        return {"ok": True, "result": result}
    except Exception as e:
        log.error("Error en send-template: %s", e)
        raise HTTPException(status_code=502, detail="Error al enviar la plantilla.")


# ── GET /api/agenda — lista todos los eventos ────────────────────────────────

@app.get("/api/agenda", response_model=list[AgendaEvent])
async def get_agenda(x_admin_key: Optional[str] = Header(default=None)) -> list[AgendaEvent]:
    _require_admin(x_admin_key)
    return _agenda_events


# ── POST /api/agenda/visita — registra una visita técnica ────────────────────

@app.post("/api/agenda/visita", response_model=AgendaBookingResponse)
async def book_visita(req: VisitaRequest, request: Request) -> AgendaBookingResponse:
    _check_rate(request)
    global _agenda_next_id

    async with _agenda_lock:
        # Verificar que el slot esté libre
        ocupados = {
            h
            for ev in _agenda_events
            if ev.fecha == req.fecha
            for h in range(ev.hora, ev.hora + ev.duracion)
        }
        if req.hora in ocupados:
            raise HTTPException(status_code=409, detail="Horario no disponible")

        event = AgendaEvent(
            id=_agenda_next_id,
            type="visita",
            fecha=req.fecha,
            hora=req.hora,
            duracion=1,
            cliente=req.nombre,
            telefono=req.telefono,
            direccion=req.direccion,
            proyecto=req.proyecto,
        )
        _agenda_next_id += 1
        _agenda_events.append(event)
        _save_agenda()

    # Evento en Odoo Calendar (no bloquea si falla)
    odoo_event_id: int | None = None
    try:
        partner_id = odoo.find_or_create_partner(
            name=req.nombre, phone=req.telefono, city=req.direccion
        )
        start_dt = f"{req.fecha} {req.hora:02d}:00:00"
        stop_dt  = f"{req.fecha} {req.hora + 1:02d}:00:00"
        odoo_event_id = odoo.create_calendar_event(
            name=f"Visita técnica — {req.nombre} ({req.proyecto})",
            start_dt=start_dt,
            stop_dt=stop_dt,
            description=f"Dirección: {req.direccion or 'No especificada'}\nTel: {req.telefono}",
            location=req.direccion or "",
            partner_id=partner_id,
        )
        log.info("Odoo calendar.event id=%s creado para %s", odoo_event_id, req.nombre)
    except Exception as exc:
        log.warning("Odoo calendar no creado (no bloquea): %s", exc)

    # WhatsApp: visita_tecnica_agendada (no bloquea si falla)
    wa_sent = False
    try:
        fecha_label = _fecha_hora_label(req.fecha, req.hora)
        wa.visita_agendada(to=req.telefono, nombre=req.nombre, fecha_hora=fecha_label)
        wa_sent = True
        log.info("WhatsApp visita_tecnica_agendada enviado a %s", req.telefono)
    except Exception as exc:
        log.warning("WhatsApp no enviado (no bloquea): %s", exc)

    return AgendaBookingResponse(
        ok=True,
        message="Visita agendada correctamente",
        event=event,
        whatsapp_sent=wa_sent,
        odoo_event_id=odoo_event_id,
    )


# ── POST /api/agenda/implementacion — bloquea slot post-compra ───────────────

@app.post("/api/agenda/implementacion", response_model=AgendaBookingResponse)
async def book_implementacion(
    req: ImplementacionRequest,
    x_admin_key: Optional[str] = Header(default=None),
) -> AgendaBookingResponse:
    _require_admin(x_admin_key)

    global _agenda_next_id

    async with _agenda_lock:
        ocupados = {
            h
            for ev in _agenda_events
            if ev.fecha == req.fecha
            for h in range(ev.hora, ev.hora + ev.duracion)
        }
        horas_bloque = set(range(req.hora, req.hora + req.duracion))
        if ocupados & horas_bloque:
            raise HTTPException(status_code=409, detail="Uno o más horarios no disponibles")

        event = AgendaEvent(
            id=_agenda_next_id,
            type="implementacion",
            fecha=req.fecha,
            hora=req.hora,
            duracion=req.duracion,
            cliente=req.cliente,
            telefono=req.telefono,
            direccion=req.direccion,
            producto=req.producto,
        )
        _agenda_next_id += 1
        _agenda_events.append(event)
        _save_agenda()

    # Evento en Odoo Calendar
    odoo_event_id = None
    try:
        partner_id = None
        if req.telefono:
            partner_id = odoo.find_or_create_partner(
                name=req.cliente, phone=req.telefono, city=req.direccion
            )
        stop_hora = req.hora + req.duracion
        start_dt  = f"{req.fecha} {req.hora:02d}:00:00"
        stop_dt   = f"{req.fecha} {stop_hora:02d}:00:00"
        odoo_event_id = odoo.create_calendar_event(
            name=f"Instalación — {req.cliente} · {req.producto or 'Digitalseg'}",
            start_dt=start_dt,
            stop_dt=stop_dt,
            description=f"Producto: {req.producto or '-'}\nDirección: {req.direccion or '-'}\nTel: {req.telefono or '-'}",
            location=req.direccion or "",
            partner_id=partner_id,
        )
        log.info("Odoo calendar.event id=%s creado (impl) para %s", odoo_event_id, req.cliente)
    except Exception as exc:
        log.warning("Odoo calendar no creado (no bloquea): %s", exc)

    # WhatsApp: instalacion_programada (solo si hay teléfono)
    wa_sent = False
    if req.telefono:
        try:
            fecha_label = _fecha_hora_label(req.fecha, req.hora)
            wa.instalacion_programada(
                to=req.telefono,
                nombre=req.cliente,
                fecha_hora=fecha_label,
                producto=req.producto or "Cerradura Digital",
                direccion=req.direccion or "",
            )
            wa_sent = True
            log.info("WhatsApp instalacion_programada enviado a %s", req.telefono)
        except Exception as exc:
            log.warning("WhatsApp impl no enviado (no bloquea): %s", exc)

    return AgendaBookingResponse(
        ok=True,
        message="Implementación registrada correctamente",
        event=event,
        whatsapp_sent=wa_sent,
        odoo_event_id=odoo_event_id,
    )


# ── Catálogo de precios oficial (fuente de verdad del servidor) ────────────────
# El cliente NUNCA define el precio; el backend lo calcula desde aquí.
PRODUCT_CATALOG: dict[str, int] = {
    # KAADAS
    "kaadas-k70-se":         989990,
    "kaadas-k20-pro":        689990,
    "kaadas-p30":            620491,
    "kaadas-q9":             589990,
    "kaadas-z1":             537990,
    "kaadas-k9-5w":          489990,
    "kaadas-k9-c5":          489990,
    "kaadas-q15":            389990,
    "kaadas-s500-black":     359990,
    "kaadas-s500-5w-cooper": 359990,
    "kaadas-s500-c5-negro":  319990,
    "kaadas-s500-c5-cobre":  319990,
    "kaadas-s110":           319990,
    "kaadas-m7w":            289990,
    "kaadas-r8-glass":       249990,
    "kaadas-s10":            237990,
    "kaadas-r8-rim":         189990,
    "kaadas-ks02a":          129990,
    # Lyon Lock
    "lyon-olimpo":           289990,
    "lyon-titan-doble":      279990,
    "lyon-titan":            259990,
    "lyon-apolo":            259990,
    "lyon-domus-wifi":       179990,
    "lyon-domus-tt":         179990,
    "lyon-pulso":            139990,
    "lyon-nexo":             129990,
    "lyon-cerrojo":          119990,
    # Accesorios / servicios
    "gateway-g2":             59990,
    "instalacion-madera":     85000,
    "instalacion-reja":      105000,
}

GATEWAY_PRICE = 59990

# ── Catálogo de cupones de descuento (fuente de verdad del servidor) ──────────
# Cargar desde COUPON_CATALOG_JSON (Railway env var) o usar defaults.
# Formato JSON: {"CODE": {"type": "percent|fixed", "value": N, "label": "..."}}
_COUPON_DEFAULTS: dict[str, dict] = {
    "LANZAMIENTO": {"type": "percent", "value": 10,    "label": "10% descuento bienvenida"},
    "PROMO15":     {"type": "percent", "value": 15,    "label": "15% descuento especial"},
    "CLIENTE10":   {"type": "percent", "value": 10,    "label": "10% descuento fidelidad"},
    "DS2025":      {"type": "fixed",   "value": 20000, "label": "$20.000 descuento"},
}
_coupon_env = os.getenv("COUPON_CATALOG_JSON", "")
if _coupon_env:
    try:
        COUPON_CATALOG: dict[str, dict] = json.loads(_coupon_env)
    except json.JSONDecodeError:
        log.warning("COUPON_CATALOG_JSON inválido — usando catálogo por defecto")
        COUPON_CATALOG = _COUPON_DEFAULTS
else:
    COUPON_CATALOG = _COUPON_DEFAULTS


# ── MercadoPago config ────────────────────────────────────────────────────────

MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "")
_mp_back_raw    = os.getenv("MP_BACK_URL", "https://digitalseg.cl").rstrip("/")
MP_BACK_URL     = _mp_back_raw if "digitalseg" in _mp_back_raw else "https://digitalseg.cl"
MP_WEBHOOK_URL  = os.getenv("MP_WEBHOOK_URL", "")
MP_API          = "https://api.mercadopago.com"


# ── POST /api/pagos/crear ─────────────────────────────────────────────────────

@app.post("/api/pagos/crear", response_model=PagoResponse)
async def crear_pago(req: PagoRequest, request: Request) -> PagoResponse:
    _check_rate(request)

    if not MP_ACCESS_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="MercadoPago no configurado. Agrega MP_ACCESS_TOKEN al backend.",
        )

    # ── Validar SKUs y calcular total desde el catálogo oficial ───────────────
    # El precio enviado por el cliente (req.precio) se ignora completamente.
    sku_list = [s.strip() for s in (req.sku or "").split(",") if s.strip()][:20]
    if not sku_list:
        raise HTTPException(status_code=400, detail="Se requiere al menos un SKU.")

    items: list[dict] = []
    total = 0
    for sku_id in sku_list:
        unit_price = PRODUCT_CATALOG.get(sku_id)
        if unit_price is None:
            raise HTTPException(status_code=400, detail=f"Producto no reconocido: {sku_id}")
        total += unit_price
        items.append({
            "id":          sku_id,
            "title":       req.producto,
            "quantity":    1,
            "unit_price":  unit_price,
            "currency_id": "CLP",
        })

    if req.gateway and "gateway-g2" not in sku_list:
        total += GATEWAY_PRICE
        items.append({
            "id":          "digitalseg-gateway-g2",
            "title":       "Gateway G2 (WiFi Bridge)",
            "quantity":    1,
            "unit_price":  GATEWAY_PRICE,
            "currency_id": "CLP",
        })

    # ── Validar cupón y aplicar descuento ────────────────────────────────────
    discount = 0
    coupon_label = ""
    if req.cupon:
        code = req.cupon.strip().upper()
        coupon = COUPON_CATALOG.get(code)
        if coupon is None:
            raise HTTPException(status_code=400, detail=f"Cupón no válido: {req.cupon}")
        if coupon["type"] == "percent":
            discount = round(total * coupon["value"] / 100)
        else:
            discount = min(int(coupon["value"]), total - 100)
        coupon_label = coupon["label"]
        if discount > 0:
            ratio = (total - discount) / total
            adjusted: list[dict] = []
            running = 0
            for i, item in enumerate(items):
                if i < len(items) - 1:
                    new_price = round(item["unit_price"] * ratio)
                    running += new_price
                    adjusted.append({**item, "unit_price": new_price})
                else:
                    adjusted.append({**item, "unit_price": max(1, (total - discount) - running)})
            items = adjusted
            total = total - discount

    ref = req.ref or f"DS-{sku_list[0]}-{int(datetime.utcnow().timestamp())}"

    preference_body: dict = {
        "items": items,
        "payer": {"name": req.cliente},
        "external_reference": ref,
        "back_urls": {
            "success": f"{MP_BACK_URL}/success.html",
            "failure": f"{MP_BACK_URL}/tienda.html",
            "pending": f"{MP_BACK_URL}/tienda.html",
        },
        "auto_return": "approved",
        "statement_descriptor": "DIGITALSEG",
        # Tope de 6 cuotas para alinear el checkout con la oferta "3 y 6 cuotas precio contado".
        # OJO: que 3 y 6 cuotas sean SIN INTERÉS (precio contado) depende de la campaña
        # "Cuotas sin interés" habilitada en la cuenta MercadoPago del vendedor;
        # esto solo limita el máximo de cuotas mostrado en el checkout.
        "payment_methods": {"installments": 6},
        "metadata": {
            "lead_id":       req.lead_id,
            "cliente":       req.cliente,
            "telefono":      req.telefono,
            "cupon":         req.cupon or "",
            "descuento_clp": discount,
        },
    }
    if MP_WEBHOOK_URL:
        preference_body["notification_url"] = MP_WEBHOOK_URL

    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{MP_API}/checkout/preferences",
                headers={
                    "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
                    "Content-Type":  "application/json",
                },
                json=preference_body,
                timeout=15,
            )
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPStatusError as exc:
        log.error("MP preferences error %s: %s", exc.response.status_code, exc.response.text)
        raise HTTPException(status_code=502, detail="Error al crear preferencia de pago. Intenta más tarde.")
    except Exception as exc:
        log.error("MP preferences unexpected error: %s", exc)
        raise HTTPException(status_code=502, detail="Error interno. Intenta más tarde.")

    log.info("MP preference creada: %s | ref=%s | total=%d", data["id"], ref, total)

    # WhatsApp: enviar link de pago (no bloquea)
    if req.telefono:
        try:
            wa.pago_link(
                to=req.telefono,
                nombre=req.cliente,
                producto=req.producto,
                total=f"${total:,}".replace(",", "."),
                link=data["init_point"],
            )
        except Exception as exc:
            log.warning("WA pago_link no enviado: %s", exc)

    # Odoo: agregar nota al lead (no bloquea)
    if req.lead_id:
        try:
            nota = (
                f"<p><b>💳 Pago iniciado</b></p>"
                f"<p>Ref: {escape(str(ref))} | Total: ${total:,} CLP | MP preference: {escape(str(data['id']))}</p>"
            )
            odoo._exec("crm.lead", "message_post", [[req.lead_id]], {"body": nota})
        except Exception as exc:
            log.warning("Odoo lead nota pago: %s", exc)

    return PagoResponse(
        ok=True,
        preference_id=data["id"],
        init_point=data["init_point"],
        sandbox_init_point=data.get("sandbox_init_point", ""),
        total=total,
        ref=ref,
    )


# ── POST /api/pagos/webhook — IPN MercadoPago ─────────────────────────────────

@app.post("/api/pagos/webhook")
async def pago_webhook(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        return {"ok": True}

    log.info("MP webhook recibido: type=%s", body.get("type"))

    if body.get("type") != "payment":
        return {"ok": True, "ignored": True}

    payment_id = str(body.get("data", {}).get("id", ""))
    if not payment_id or not MP_ACCESS_TOKEN:
        return {"ok": True}

    # Verificar firma del webhook (fail-closed: sin MP_WEBHOOK_SECRET se rechaza)
    if not _verify_mp_signature(request, payment_id):
        log.warning("MP webhook rechazado: firma inválida o MP_WEBHOOK_SECRET ausente")
        raise HTTPException(status_code=401, detail="Firma inválida")

    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"{MP_API}/v1/payments/{payment_id}",
                headers={"Authorization": f"Bearer {MP_ACCESS_TOKEN}"},
                timeout=15,
            )
            r.raise_for_status()
            payment = r.json()
    except Exception as exc:
        log.error("Error consultando pago %s: %s", payment_id, exc)
        raise HTTPException(status_code=502, detail="Error procesando el pago.")

    status       = payment.get("status", "")
    metadata     = payment.get("metadata", {})
    cliente      = metadata.get("cliente", "")
    telefono     = metadata.get("telefono", "")
    lead_id_raw  = metadata.get("lead_id")
    ext_ref      = payment.get("external_reference", "")

    log.info("Pago %s: status=%s | cliente=***%s | ref=%s", payment_id, status, cliente[-3:] if cliente else "?", ext_ref)

    if status == "approved":
        # Anti-replay: no repetir efectos secundarios de un pago ya procesado
        if payment_id in _processed_payments:
            return {"ok": True, "payment_id": payment_id, "status": status, "dedup": True}
        _processed_payments.add(payment_id)
        if len(_processed_payments) > 5000:
            _processed_payments.clear()
        # Odoo: agregar nota de pago confirmado + intentar marcar como ganado
        if lead_id_raw:
            try:
                lead_id = int(lead_id_raw)
                nota = (
                    f"<p><b>✅ Pago aprobado</b></p>"
                    f"<p>payment_id: {escape(str(payment_id))} | ref: {escape(str(ext_ref))}</p>"
                )
                odoo._exec("crm.lead", "message_post", [[lead_id]], {"body": nota})
                try:
                    odoo._exec("crm.lead", "action_set_won_rainbowman", [[lead_id]])
                except Exception:
                    pass
            except Exception as exc:
                log.warning("Odoo update pago aprobado: %s", exc)

        # WhatsApp: confirmar pago al cliente
        if telefono:
            try:
                wa.pago_confirmado(to=telefono, nombre=cliente)
            except Exception as exc:
                log.warning("WA pago_confirmado no enviado: %s", exc)

    return {"ok": True, "payment_id": payment_id, "status": status}


# ── Email helpers ─────────────────────────────────────────────────────────────

_SPACE_LABELS = {
    "casa": "Casa", "departamento": "Departamento", "oficina": "Oficina",
    "local": "Local comercial", "airbnb": "Arriendo / Airbnb", "bodega": "Bodega",
    "parcela": "Parcela / Reja", "cajon": "Cajón / Mueble",
}
_DOOR_LABELS = {
    "madera": "Puerta de madera", "metal": "Puerta de metal", "reja": "Reja / Fierro",
    "vidrio": "Puerta de vidrio", "cajon": "Cajón / Mueble",
}


def _build_lead_html(c, rec, req, quantity: int, install_label: str,
                     install_price: float, lead_url: str, sale_url: str,
                     source: str) -> str:
    """Correo INTERNO para el equipo (Sebastián): resumen del lead para el
    seguimiento, con botón de WhatsApp directo al cliente. No es de venta."""
    nombre = escape(str(c.nombre))
    ciudad = escape(str(c.ciudad or "—"))
    email  = escape(str(c.email or "—"))
    tel     = escape(str(c.telefono or "—"))
    wa_digits = "".join(ch for ch in str(c.telefono or "") if ch.isdigit())
    producto = escape(f"{rec.brand} {rec.name}")
    sku      = escape(str(rec.sku))
    precio   = f"${rec.price:,.0f}".replace(",", ".")
    inst     = (f"{escape(install_label)} · ${install_price:,.0f}".replace(",", ".")
                if install_price else "No especificada")
    space = _SPACE_LABELS.get((req.space or "").lower(), escape(str(req.space or "—")))
    door  = _DOOR_LABELS.get((req.doorType or "").lower(), escape(str(req.doorType or "—")))
    if req.budgetMax and req.budgetMax < 9_999_999:
        pmin = f"${(req.budgetMin or 0):,.0f}".replace(",", ".")
        pmax = f"${req.budgetMax:,.0f}".replace(",", ".")
        presup = f"{pmin} – {pmax}"
    else:
        presup = "Sin definir"
    formal = ""
    if getattr(c, "cotizacionFormal", False):
        formal = (f'<tr><td style="padding:6px 0;color:#7a91a9">Cotización formal</td>'
                  f'<td style="padding:6px 0;color:#0a1b33;font-weight:600">Sí · {escape(str(c.razonSocial or ""))} '
                  f'{escape(str(c.rut or ""))}</td></tr>')
    wa_link = (f"https://wa.me/{wa_digits}?text=" +
               f"Hola%20{nombre.split(' ')[0]}%2C%20soy%20Sebasti%C3%A1n%20de%20DigitalSeg.%20"
               f"Vi%20tu%20cotizaci%C3%B3n%20de%20la%20{producto.replace(' ', '%20')}%20y%20quiero%20ayudarte%20a%20avanzar."
               ) if wa_digits else ""
    wa_btn = (f'<a href="{wa_link}" style="display:inline-block;background:#25D366;color:#fff;'
              f'text-decoration:none;padding:12px 26px;border-radius:40px;font-weight:800;font-size:15px">'
              f'💬 Escribir a {nombre.split(" ")[0]} por WhatsApp</a>') if wa_link else ""

    def row(label, value):
        return (f'<tr><td style="padding:6px 0;color:#7a91a9;white-space:nowrap;vertical-align:top">{label}</td>'
                f'<td style="padding:6px 0;color:#0a1b33;font-weight:600">{value}</td></tr>')

    return f"""<!DOCTYPE html><html lang="es"><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#eef2f6;font-family:Arial,Helvetica,sans-serif">
<div style="max-width:600px;margin:0 auto;background:#fff">
  <div style="background:#0a1b33;padding:22px 28px">
    <p style="margin:0 0 3px;color:#7ee097;font-size:11px;letter-spacing:.14em;font-weight:700;text-transform:uppercase">🔔 Nuevo lead del cotizador</p>
    <h1 style="margin:0;color:#fff;font-size:22px;font-weight:900">{nombre}</h1>
    <p style="margin:4px 0 0;color:#8fa6bd;font-size:12px">Origen: {escape(str(source or "cotizador web"))}</p>
  </div>
  <div style="padding:24px 28px">
    <div style="text-align:center;margin:0 0 22px">{wa_btn}</div>

    <p style="margin:0 0 6px;font-size:11px;letter-spacing:.06em;color:#7a91a9;text-transform:uppercase;font-weight:700">Contacto</p>
    <table style="width:100%;border-collapse:collapse;font-size:14px;margin-bottom:20px">
      {row("WhatsApp", tel)}
      {row("Email", email)}
      {row("Ciudad", ciudad)}
    </table>

    <p style="margin:0 0 6px;font-size:11px;letter-spacing:.06em;color:#7a91a9;text-transform:uppercase;font-weight:700">Qué cotizó</p>
    <table style="width:100%;border-collapse:collapse;font-size:14px;margin-bottom:20px">
      {row("Producto", producto)}
      {row("SKU", sku)}
      {row("Cantidad", quantity)}
      {row("Precio unitario", precio)}
      {row("Instalación", inst)}
      {formal}
    </table>

    <p style="margin:0 0 6px;font-size:11px;letter-spacing:.06em;color:#7a91a9;text-transform:uppercase;font-weight:700">Contexto de la puerta</p>
    <table style="width:100%;border-collapse:collapse;font-size:14px;margin-bottom:20px">
      {row("Espacio", space)}
      {row("Tipo de puerta", door)}
      {row("Presupuesto", presup)}
      {row("Grosor / medidas", "A confirmar en visita técnica")}
    </table>

    <div style="background:#f6f9fc;border:1px solid #dfe8f1;border-radius:10px;padding:14px 16px;font-size:13px">
      <a href="{lead_url}" style="color:#3f7fc4;font-weight:700">Ver lead en Odoo →</a>
      {'&nbsp;·&nbsp; <a href="' + sale_url + '" style="color:#3f7fc4;font-weight:700">Ver cotización →</a>' if sale_url else ''}
    </div>
  </div>
  <div style="background:#0a1b33;padding:12px 28px;text-align:center;font-size:11px;color:#6f88a3">
    DigitalSeg · notificación interna de lead
  </div>
</div>
</body></html>"""


def _build_cotizacion_html(
    nombre: str,
    producto: str,
    sku: str,
    price: float,
    quantity: int,
    in_stock: bool,
    delivery_days: int,
    odoo_url: str,
    install_price: float = 0,
    install_label: str = "",
) -> str:
    nombre   = escape(str(nombre))
    producto = escape(str(producto))
    sku      = escape(str(sku))
    total = price * quantity
    stock_badge = (
        "<span style='background:#27ae60;color:#fff;padding:3px 10px;border-radius:20px;font-size:12px;font-weight:bold'>✓ En stock</span>"
        if in_stock else
        "<span style='background:#f39c12;color:#fff;padding:3px 10px;border-radius:20px;font-size:12px;font-weight:bold'>⏳ Por pedido</span>"
    )
    precio_fmt  = f"${price:,.0f}".replace(",", ".")
    total_fmt   = f"${total:,.0f}".replace(",", ".")
    wa_url = "https://wa.me/56946880196?text=Hola%2C+acabo+de+recibir+mi+cotizaci%C3%B3n+y+quiero+avanzar"
    install_fmt = f"${install_price:,.0f}".replace(",", ".")
    grand_total_fmt = f"${total + install_price:,.0f}".replace(",", ".")
    if install_price:
        install_block = (
            '<div style="background:#fff8e1;border-left:4px solid #f5a623;border-radius:0 8px 8px 0;padding:16px;margin-bottom:20px">'
            f'<p style="margin:0 0 8px;font-size:14px;color:#7a5a00;font-weight:700">🔧 {install_label}</p>'
            '<table style="width:100%;border-collapse:collapse;font-size:13px;color:#7a5a00">'
            f'<tr><td style="padding:3px 0">Producto</td><td style="padding:3px 0;text-align:right">{total_fmt}</td></tr>'
            f'<tr><td style="padding:3px 0">Instalación profesional</td><td style="padding:3px 0;text-align:right">{install_fmt}</td></tr>'
            f'<tr><td style="padding:8px 0 0;font-weight:bold;border-top:1px solid #f0d98a">Total con instalación</td><td style="padding:8px 0 0;text-align:right;font-weight:900;font-size:16px;color:#1b5e20;border-top:1px solid #f0d98a">{grand_total_fmt}</td></tr>'
            '</table>'
            '<p style="margin:10px 0 0;font-size:12px;color:#9a7a00">Instalación obligatoria: es parte de tu garantía de 12 meses.</p>'
            '</div>'
        )
    else:
        install_block = (
            '<div style="background:#fff8e1;border-left:4px solid #f5a623;border-radius:0 8px 8px 0;padding:16px;margin-bottom:20px">'
            '<p style="margin:0;font-size:13px;color:#7a5a00;line-height:1.6">🔧 La instalación profesional es <strong>obligatoria</strong> (garantía 12 meses): <strong>$89.990</strong> madera · <strong>$99.990</strong> reja/fierro.</p>'
            '</div>'
        )

    # ── Formas de pago (palanca neuroventas, misma lógica de la experiencia) ──
    grand_total = total + install_price
    def _clp(n: float) -> str:
        return f"${round(n):,.0f}".replace(",", ".")
    contado_fmt = _clp(grand_total * 0.95)
    cuota3_fmt  = _clp(grand_total / 3)
    cuota6_fmt  = _clp(grand_total / 6)
    neto_fmt    = _clp(grand_total / 1.19)
    iva_fmt     = _clp(grand_total - grand_total / 1.19)
    base_ref    = _clp(grand_total)
    payment_block = (
        '<p style="margin:2px 0 10px;font-size:12px;letter-spacing:.05em;color:#7a91a9;text-transform:uppercase;font-weight:700">Elige cómo pagar</p>'
        '<table style="width:100%;border-collapse:separate;border-spacing:0 8px;font-size:14px;margin-bottom:14px">'
        f'<tr><td style="background:#eafaf0;padding:13px 15px;border-radius:8px 0 0 8px;color:#0f5a30;font-weight:700">💚 Contado <span style="font-weight:400;color:#2f7a4f">· 5% dcto.</span></td>'
        f'<td style="background:#eafaf0;padding:13px 15px;border-radius:0 8px 8px 0;text-align:right;font-weight:900;color:#1b8f4d">{contado_fmt}</td></tr>'
        f'<tr><td style="background:#f6f9fc;padding:13px 15px;border-radius:8px 0 0 8px;color:#0a1b33;font-weight:700">💳 3 cuotas sin interés</td>'
        f'<td style="background:#f6f9fc;padding:13px 15px;border-radius:0 8px 8px 0;text-align:right;font-weight:900;color:#0a1b33">3 × {cuota3_fmt}</td></tr>'
        f'<tr><td style="background:#f6f9fc;padding:13px 15px;border-radius:8px 0 0 8px;color:#0a1b33;font-weight:700">💳 6 cuotas sin interés</td>'
        f'<td style="background:#f6f9fc;padding:13px 15px;border-radius:0 8px 8px 0;text-align:right;font-weight:900;color:#0a1b33">6 × {cuota6_fmt}</td></tr>'
        f'<tr><td style="background:#f6f9fc;padding:13px 15px;border-radius:8px 0 0 8px;color:#0a1b33;font-weight:700">🧾 Con factura <span style="font-weight:400;color:#5a6b7c">· IVA recuperable</span></td>'
        f'<td style="background:#f6f9fc;padding:13px 15px;border-radius:0 8px 8px 0;text-align:right;font-weight:700;color:#0a1b33">Neto {neto_fmt}<br><span style="font-weight:400;color:#7a91a9;font-size:12px">+ IVA {iva_fmt}</span></td></tr>'
        '</table>'
    )
    trust_bar = (
        '<div style="background:#0a1b33;border-radius:12px;padding:15px 18px;margin:6px 0 22px;text-align:center">'
        '<span style="color:#cfe3f6;font-size:12.5px;line-height:1.9">'
        '🛡️ Garantía de 12 meses &nbsp;·&nbsp; 📍 +100 instalaciones en el Aconcagua &nbsp;·&nbsp; 🔧 Técnico certificado'
        '</span></div>'
    )
    return f"""<!DOCTYPE html><html lang="es"><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#071426;font-family:Arial,Helvetica,sans-serif">
<div style="max-width:600px;margin:0 auto;background:#fff">

  <!-- Header oscuro premium -->
  <div style="background:#0a1b33;padding:34px 32px;text-align:center;border-bottom:3px solid #7ee097">
    <p style="margin:0 0 6px;color:#9ed0ff;font-size:11px;letter-spacing:.18em;font-weight:700;text-transform:uppercase">Tu cotización personalizada</p>
    <h1 style="margin:0;color:#fff;font-size:27px;font-weight:900;letter-spacing:.02em">DIGITAL<span style="color:#7ee097">SEG</span></h1>
    <p style="margin:8px 0 0;color:#8fa6bd;font-size:12px;letter-spacing:.04em">Seguridad Inteligente · Valle del Aconcagua</p>
  </div>

  <!-- Saludo + gancho neuroventas -->
  <div style="padding:32px 32px 8px">
    <h2 style="margin:0 0 10px;font-size:22px;color:#0a1b33;font-weight:800">Hola, {nombre} 👋</h2>
    <p style="margin:0 0 18px;color:#4a5a6a;font-size:15px;line-height:1.65">
      Preparé esta propuesta pensando en una sola cosa: <strong style="color:#0a1b33">tu tranquilidad</strong>.
      La llave es el eslabón más débil de tu puerta — se copia, se pierde, se olvida. Mira lo que cambia el día que la dejas atrás.
    </p>
  </div>

  <!-- Banda dolor → beneficio (fondo oscuro, estilo cotización) -->
  <div style="padding:4px 32px 8px">
    <div style="background:#0a1b33;border-radius:14px;padding:22px 22px 8px">
      <table style="width:100%;border-collapse:collapse">
        <tr>
          <td style="padding:0 0 14px;color:#ff9e9e;font-size:14px;line-height:1.5;vertical-align:top;width:50%">✕ Copias de llave que no controlas</td>
          <td style="padding:0 0 14px;color:#7ee097;font-size:14px;line-height:1.5;vertical-align:top;width:50%;padding-left:14px">✓ Tú decides quién entra y cuándo</td>
        </tr>
        <tr>
          <td style="padding:0 0 14px;color:#ff9e9e;font-size:14px;line-height:1.5;vertical-align:top">✕ El "¿cerré bien?" a mitad de camino</td>
          <td style="padding:0 0 14px;color:#7ee097;font-size:14px;line-height:1.5;vertical-align:top;padding-left:14px">✓ Lo confirmas desde el celular</td>
        </tr>
        <tr>
          <td style="padding:0 0 14px;color:#ff9e9e;font-size:14px;line-height:1.5;vertical-align:top">✕ Llegar de noche buscando la llave</td>
          <td style="padding:0 0 14px;color:#7ee097;font-size:14px;line-height:1.5;vertical-align:top;padding-left:14px">✓ Entras con tu huella, en un segundo</td>
        </tr>
      </table>
    </div>
  </div>

  <div style="padding:20px 32px 32px">
    <!-- Tarjeta de producto -->
    <p style="margin:6px 0 10px;font-size:12px;letter-spacing:.05em;color:#7a91a9;text-transform:uppercase;font-weight:700">Esto recomendamos para ti</p>
    <div style="background:#f6f9fc;border:1px solid #dfe8f1;border-radius:14px;padding:24px;margin-bottom:22px">
      <div style="margin-bottom:12px">{stock_badge}</div>
      <h3 style="margin:0 0 4px;font-size:18px;color:#0a1b33;font-weight:800">{producto}</h3>
      <p style="margin:0 0 16px;color:#9fb4c9;font-size:12px;letter-spacing:.05em">SKU: {sku}</p>

      <table style="width:100%;border-collapse:collapse;font-size:14px">
        <tr>
          <td style="padding:8px 0;color:#5a6b7c;border-bottom:1px solid #e6edf4">Precio unitario</td>
          <td style="padding:8px 0;text-align:right;font-weight:700;color:#0a1b33;border-bottom:1px solid #e6edf4">{precio_fmt}</td>
        </tr>
        <tr>
          <td style="padding:8px 0;color:#5a6b7c;border-bottom:1px solid #e6edf4">Cantidad</td>
          <td style="padding:8px 0;text-align:right;font-weight:700;color:#0a1b33;border-bottom:1px solid #e6edf4">{quantity}</td>
        </tr>
        <tr>
          <td style="padding:12px 0 0;color:#0a1b33;font-weight:bold;font-size:16px">Total</td>
          <td style="padding:12px 0 0;text-align:right;font-weight:900;font-size:22px;color:#1b8f4d">{total_fmt}</td>
        </tr>
      </table>
    </div>

    <!-- Entrega / urgencia -->
    <div style="background:#eafaf0;border-left:4px solid #7ee097;border-radius:0 8px 8px 0;padding:16px;margin-bottom:20px">
      <p style="margin:0;font-size:14px;color:#0f5a30;line-height:1.55">
        <strong>🚚 Entrega e instalación estimada:</strong> {delivery_days} días hábiles
        {"— producto disponible en bodega, agendamos altiro." if in_stock else "— lo conseguimos a pedido y coordinamos la fecha contigo."}
      </p>
    </div>

    {install_block}

    {payment_block}

    <!-- Reencuadre de valor -->
    <div style="background:#0a1b33;border-radius:12px;padding:20px 22px;margin:4px 0 22px">
      <p style="margin:0;color:#cfe3f6;font-size:14px;line-height:1.6">
        No es un gasto: es dejar de depender de un pedazo de metal que cualquiera puede copiar.
        <span style="color:#7ee097;font-weight:700">Compras una sola vez la tranquilidad de todos los días.</span>
      </p>
    </div>

    {trust_bar}

    <p style="font-size:12px;color:#9fb4c9;margin:0 0 22px;line-height:1.6">
      Cotización referencial. <a href="{odoo_url}" style="color:#3f7fc4">Ver cotización en sistema →</a>
    </p>

    <!-- Cierre personal + CTA -->
    <div style="text-align:center;padding:6px 0 4px">
      <p style="margin:0 0 16px;font-size:17px;color:#0a1b33;font-weight:800;line-height:1.4">{nombre}, tu propuesta está lista.<br>Solo falta tu sí. 💚</p>
      <a href="{wa_url}"
         style="display:inline-block;background:#25D366;color:#fff;text-decoration:none;padding:15px 34px;border-radius:50px;font-weight:800;font-size:16px">
        💬 Hablar con Sebastián ahora
      </a>
      <p style="font-size:12px;color:#9fb4c9;margin:12px 0 0">
        O escríbele directo al +56 9 4688 0196
      </p>
    </div>

    <!-- Firma -->
    <div style="margin-top:26px;padding-top:20px;border-top:1px solid #e6edf4">
      <p style="margin:0;font-size:15px;color:#0a1b33;font-weight:800">Sebastián Cabrera</p>
      <p style="margin:3px 0 0;font-size:13px;color:#7a91a9">Tu asesor de seguridad · DigitalSeg</p>
      <p style="margin:3px 0 0;font-size:13px;color:#7a91a9">No estás comprando a un desconocido: te acompaño desde la elección hasta la instalación.</p>
      <p style="margin:8px 0 0;font-size:13px;color:#7a91a9">📱 +56 9 4688 0196 &nbsp;·&nbsp; ✉️ sebastian.cabrera@digitalseg.cl</p>
    </div>
  </div>

  <div style="background:#0a1b33;padding:16px 32px;text-align:center;font-size:11px;color:#6f88a3">
    DigitalSeg · Seguridad Inteligente · Valle del Aconcagua · digitalseg.cl
  </div>
</div>
</body></html>"""


# ── SMTP forzando IPv4 ────────────────────────────────────────────────────────
# Railway no tiene ruta IPv6 y smtp.hostinger.com resuelve AAAA (Cloudflare) →
# "Network is unreachable". Resolvemos solo el registro A y conectamos por IPv4;
# el TLS sigue validando el certificado contra el hostname (server_hostname).
def _resolve_ipv4(host: str, port: int) -> str:
    return socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]


class _SMTP_IPv4(smtplib.SMTP):
    def _get_socket(self, host, port, timeout):
        return socket.create_connection((_resolve_ipv4(host, port), port), timeout, self.source_address)


class _SMTP_SSL_IPv4(smtplib.SMTP_SSL):
    def _get_socket(self, host, port, timeout):
        sock = socket.create_connection((_resolve_ipv4(host, port), port), timeout, self.source_address)
        return self.context.wrap_socket(sock, server_hostname=self._host)


def _resend_from() -> str:
    return os.getenv("RESEND_FROM", "").strip() or "DigitalSeg · Sebastián Cabrera <notificaciones@digitalseg.cl>"


def _send_via_resend(subject: str, html: str, to_addresses: list[str]) -> tuple[bool, str]:
    """Envía por la API HTTP de Resend (puerto 443). Devuelve (ok, detalle)."""
    api_key = os.getenv("RESEND_API_KEY", "").strip()
    if not api_key:
        return False, "sin RESEND_API_KEY"
    try:
        r = httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"from": _resend_from(), "to": to_addresses, "subject": subject, "html": html},
            timeout=20,
        )
        if r.status_code < 300:
            return True, r.text[:200]
        return False, f"HTTP {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:200]}"


def _send_email(subject: str, html: str, to_addresses: list[str]) -> None:
    # Railway no alcanza el SMTP de Hostinger (IPv6 sin ruta / puerto 465 timeout).
    # Orden de transporte: 1) Odoo (su servidor saliente YA funciona para
    # digitalseg.cl), 2) Resend (HTTPS, si hay API key), 3) SMTP (respaldo).
    _odoo = globals().get("odoo")
    if _odoo is not None:
        try:
            _odoo.send_html_email(
                subject, html, to_addresses,
                email_from=os.getenv("ODOO_MAIL_FROM", "").strip(),
            )
            log.info("Email (Odoo) enviado a %s: %s", to_addresses, subject)
            return
        except Exception as e:
            log.error("Envío por Odoo falló (%s) — intento Resend/SMTP", e)

    if os.getenv("RESEND_API_KEY", "").strip():
        ok, info = _send_via_resend(subject, html, to_addresses)
        if ok:
            log.info("Email (Resend) enviado a %s: %s", to_addresses, subject)
            return
        log.error("Resend falló (%s) — intento SMTP de respaldo", info)

    host = os.getenv("SMTP_HOST", "smtp.hostinger.com").strip() or "smtp.hostinger.com"
    port = int((os.getenv("SMTP_PORT", "587") or "587").strip())
    user = os.getenv("SMTP_USER", "").strip()
    pwd  = os.getenv("SMTP_PASS", "").strip()
    if not user or not pwd:
        log.warning("Email no enviado — ni Resend ni SMTP configurados")
        return
    from_name = os.getenv("SMTP_FROM_NAME", "DigitalSeg · Sebastián Cabrera")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = formataddr((str(Header(from_name, "utf-8")), user))
    msg["To"]      = ", ".join(to_addresses)
    msg.attach(MIMEText(html, "html", "utf-8"))
    # Puerto 465 = SSL directo; 587 (u otro) = STARTTLS
    if port == 465:
        with _SMTP_SSL_IPv4(host, port, timeout=25) as s:
            s.login(user, pwd)
            s.sendmail(user, to_addresses, msg.as_string())
    else:
        with _SMTP_IPv4(host, port, timeout=25) as s:
            s.ehlo()
            s.starttls()
            s.login(user, pwd)
            s.sendmail(user, to_addresses, msg.as_string())
    log.info("Email enviado a %s: %s", to_addresses, subject)


# ── POST /api/informe-seguridad ───────────────────────────────────────────────

_INFORME_RECIPIENTS = ["sebastian.cabrera@digitalseg.cl", "contacto@digitalseg.cl"]

@app.post("/api/informe-seguridad")
async def informe_seguridad(req: InformeSeguridad, request: Request) -> dict:
    _check_rate(request)

    nivel_color = {"Bajo": "#e74c3c", "Medio": "#f39c12", "Alto": "#27ae60"}.get(req.nivel, "#555")

    findings_html = "".join(f"<li>{escape(str(f))}</li>" for f in req.findings) if req.findings else "<li>Sin hallazgos críticos</li>"
    recs_html     = "".join(f"<li>{escape(str(r))}</li>" for r in req.recs)     if req.recs     else "<li>Mantener el nivel actual</li>"
    respuestas_html = "".join(
        f"<tr><td style='padding:6px 12px;border-bottom:1px solid #eee'>{escape(str(k))}</td>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #eee'><b>{escape(str(v))}</b></td></tr>"
        for k, v in req.respuestas.items()
    )

    html = f"""
<!DOCTYPE html><html lang="es"><head><meta charset="utf-8"></head>
<body style="font-family:Arial,sans-serif;background:#f5f5f5;padding:24px;margin:0">
<div style="max-width:620px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,.1)">
  <div style="background:#0a0a0a;padding:28px 32px;text-align:center">
    <h1 style="color:#fff;margin:0;font-size:22px">DigitalSeg — Informe de Seguridad</h1>
    <p style="color:#aaa;margin:6px 0 0;font-size:14px">Calculadora de Seguridad Inteligente</p>
  </div>
  <div style="padding:28px 32px">
    <h2 style="margin:0 0 4px;font-size:18px">Hola, {escape(req.nombre)}</h2>
    <p style="color:#555;margin:0 0 20px;font-size:14px">
      Tel: {escape(req.telefono)}{f" | Email: {escape(req.email)}" if req.email else ""}
      {f" | Zona: {escape(req.zona)}" if req.zona else ""}
      {f" | Propiedad: {escape(req.tipo_propiedad)}" if req.tipo_propiedad else ""}
    </p>

    <div style="text-align:center;margin:20px 0">
      <div style="display:inline-block;background:#f9f9f9;border-radius:50%;width:100px;height:100px;line-height:100px;font-size:36px;font-weight:bold;color:{nivel_color};border:4px solid {nivel_color}">
        {req.score}
      </div>
      <p style="margin:10px 0 0;font-size:18px;font-weight:bold;color:{nivel_color}">Nivel {req.nivel}</p>
    </div>

    <h3 style="color:#0a0a0a;border-bottom:2px solid #eee;padding-bottom:8px">⚠️ Hallazgos</h3>
    <ul style="color:#555;padding-left:20px;line-height:1.8">{findings_html}</ul>

    <h3 style="color:#0a0a0a;border-bottom:2px solid #eee;padding-bottom:8px">✅ Recomendaciones</h3>
    <ul style="color:#555;padding-left:20px;line-height:1.8">{recs_html}</ul>

    {"<h3 style='color:#0a0a0a;border-bottom:2px solid #eee;padding-bottom:8px'>📋 Respuestas del cuestionario</h3><table style='width:100%;border-collapse:collapse;font-size:13px'>" + respuestas_html + "</table>" if req.respuestas else ""}
  </div>
  <div style="background:#f5f5f5;padding:16px 32px;text-align:center;font-size:12px;color:#888">
    DigitalSeg — Cerraduras inteligentes Valle del Aconcagua · digitalseg.cl
  </div>
</div>
</body></html>
"""

    subject = f"[Informe] {req.nombre} — Score {req.score} ({req.nivel})"

    try:
        _send_email(subject=subject, html=html, to_addresses=_INFORME_RECIPIENTS)
    except Exception as exc:
        log.error("Error enviando informe de seguridad: %s", exc)

    # Crear lead en Odoo (no bloquea)
    try:
        partner_id = odoo.find_or_create_partner(
            name=req.nombre,
            phone=req.telefono,
            city=req.zona,
        )
        desc = f"Score: {req.score} | Nivel: {req.nivel}\nZona: {req.zona or '-'} | Propiedad: {req.tipo_propiedad or '-'}"
        odoo.create_lead(
            partner_id=partner_id,
            phone=req.telefono,
            product_name="Calculadora Seguridad",
            sku="CALC-SEG",
            price=0,
            requirements=req.respuestas,
            source_label="calculadora",
            quantity=1,
            needs_gateway=False,
            email=req.email,
        )
        log.info("Lead Odoo creado desde calculadora para %s", req.nombre)
    except Exception as exc:
        log.warning("Odoo lead calculadora no creado (no bloquea): %s", exc)

    return {"ok": True, "message": "Informe enviado"}


# ── Health ──────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict:
    return {
        "status":    "ok",
        "odoo":      "connected",
        "whatsapp":  wa._configured,
        "mercadopago": bool(MP_ACCESS_TOKEN),
    }
