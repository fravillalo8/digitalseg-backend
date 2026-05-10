from __future__ import annotations
import os
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from models import LeadPayload, LeadResponse, SendTemplateRequest
from odoo_client import OdooClient
from whatsapp_client import WhatsAppClient

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger(__name__)

odoo: OdooClient
wa: WhatsAppClient


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global odoo, wa
    odoo = OdooClient()
    wa   = WhatsAppClient()
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


# ── Health ──────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "odoo": "connected", "whatsapp": wa._configured}
