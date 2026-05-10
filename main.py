from __future__ import annotations
import os
import json
import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import AsyncIterator

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, HTTPException, Header, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from models import (
    LeadPayload, LeadResponse, SendTemplateRequest,
    VisitaRequest, ImplementacionRequest, AgendaEvent, AgendaBookingResponse,
)
from odoo_client import OdooClient
from whatsapp_client import WhatsAppClient

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger(__name__)

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

app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.getenv("DIGITALSEG_ALLOWED_ORIGIN", "https://digitalseg.cl")],
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["Content-Type"],
)


# ── POST /api/leads ─────────────────────────────────────────────────────────────

@app.post("/api/leads", response_model=LeadResponse)
async def create_lead(payload: LeadPayload) -> LeadResponse:
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
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Error creando contacto en Odoo: {e}")

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
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Error creando oportunidad en Odoo: {e}")

    # 3. Cotización formal (si la pidió)
    sale_order_id = None
    if c.cotizacionFormal:
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
            )
        except Exception as e:
            log.error("Error creando cotización: %s", e)

    # 4. WhatsApp: confirmar solicitud recibida
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
        params.get("hub.mode") == "subscribe"
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
        log.info("WA entrante de %s (%s): %s", msg["from"], msg["name"], msg["text"])
        _update_lead_from_whatsapp(msg)

    return {"status": "ok"}


def _update_lead_from_whatsapp(msg: dict) -> None:
    """Agrega nota al lead de Odoo que coincide con el número de teléfono."""
    phone = f"+{msg['from']}" if not msg["from"].startswith("+") else msg["from"]
    try:
        ids = odoo._exec(
            "crm.lead", "search",
            [[["phone", "=", phone], ["active", "=", True]]],
            {"limit": 1},
        )
        if not ids:
            log.info("Sin lead activo para %s — mensaje ignorado", phone)
            return

        note = (
            f"<p><b>Mensaje WhatsApp</b> de {msg['name']} ({phone})</p>"
            f"<p>{msg['text']}</p>"
        )
        odoo._exec("crm.lead", "write", [[ids[0]], {"description": note}])
        log.info("Lead id=%s actualizado con mensaje WA", ids[0])
    except Exception as e:
        log.error("Error actualizando lead desde WA: %s", e)


# ── POST /api/whatsapp/send-template ────────────────────────────────────────────

@app.post("/api/whatsapp/send-template")
async def send_template(req: SendTemplateRequest) -> dict:
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
        raise HTTPException(status_code=502, detail=str(e))


# ── GET /api/agenda — lista todos los eventos ────────────────────────────────

@app.get("/api/agenda", response_model=list[AgendaEvent])
async def get_agenda() -> list[AgendaEvent]:
    return _agenda_events


# ── POST /api/agenda/visita — registra una visita técnica ────────────────────

@app.post("/api/agenda/visita", response_model=AgendaBookingResponse)
async def book_visita(req: VisitaRequest) -> AgendaBookingResponse:
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
    x_admin_key: str | None = Header(default=None),
) -> AgendaBookingResponse:
    admin_key = os.getenv("AGENDA_ADMIN_KEY", "")
    if admin_key and x_admin_key != admin_key:
        raise HTTPException(status_code=401, detail="X-Admin-Key inválido")

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
            direccion=req.direccion,
            producto=req.producto,
        )
        _agenda_next_id += 1
        _agenda_events.append(event)
        _save_agenda()

    # Evento en Odoo Calendar
    odoo_event_id = None
    try:
        stop_hora = req.hora + req.duracion
        start_dt  = f"{req.fecha} {req.hora:02d}:00:00"
        stop_dt   = f"{req.fecha} {stop_hora:02d}:00:00"
        odoo_event_id = odoo.create_calendar_event(
            name=f"Instalación — {req.cliente} · {req.producto or 'Digitalseg'}",
            start_dt=start_dt,
            stop_dt=stop_dt,
            description=f"Producto: {req.producto or '-'}\nDirección: {req.direccion or '-'}",
            location=req.direccion or "",
        )
        log.info("Odoo calendar.event id=%s creado (impl) para %s", odoo_event_id, req.cliente)
    except Exception as exc:
        log.warning("Odoo calendar no creado (no bloquea): %s", exc)

    return AgendaBookingResponse(
        ok=True,
        message="Implementación registrada correctamente",
        event=event,
        odoo_event_id=odoo_event_id,
    )


# ── Health ──────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "odoo": "connected", "whatsapp": wa._configured}
