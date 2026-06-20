"""
Script de un solo uso para Odoo (DigitalSeg).
Ejecutar en Railway, donde las credenciales ya funcionan:

    cd backend
    railway link                 # seleccionar el proyecto digitalseg-backend
    railway run python fix_quotation_template.py

Hace 3 cosas:
  1) Reemplaza "quotation" -> "cotización" en las plantillas de sale.order.
  2) Pone un mensaje humano (saludo por nombre) + firma de Sebastián Cabrera
     en la plantilla de presupuesto que se envía al cliente.
  3) Sube el correlativo de presupuestos para que el próximo sea S00168.
"""
from __future__ import annotations
import os, re, xmlrpc.client

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

URL    = os.environ["ODOO_URL"]
DB     = os.environ["ODOO_DB"]
USER   = os.environ["ODOO_USER"]
APIKEY = os.environ["ODOO_APIKEY"]

common = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/common")
uid    = common.authenticate(DB, USER, APIKEY, {})
if not uid:
    raise SystemExit("❌  Autenticación fallida — verifica las credenciales (ODOO_*)")
models = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/object")


def ex(model, method, args, kwargs={}):
    return models.execute_kw(DB, uid, APIKEY, model, method, args, kwargs)


# ── 1) quotation -> cotización en todas las plantillas de sale.order ──────────
templates = ex(
    "mail.template", "search_read",
    [[["model", "=", "sale.order"]]],
    {"fields": ["id", "name", "subject", "body_html"]},
)
print(f"Plantillas de sale.order encontradas: {len(templates)}")

REPL = [("quotation", "cotización"), ("Quotation", "Cotización"), ("QUOTATION", "COTIZACIÓN")]
for t in templates:
    nb = t.get("body_html") or ""
    ns = t.get("subject") or ""
    for pat, rep in REPL:
        nb = re.sub(pat, rep, nb)
        ns = re.sub(pat, rep, ns)
    if nb != (t.get("body_html") or "") or ns != (t.get("subject") or ""):
        ex("mail.template", "write", [[t["id"]], {"body_html": nb, "subject": ns}])
        print(f"  ✅ [{t['id']}] {t['name']} → 'quotation' traducido")


# ── 2) Mensaje humano + firma en la plantilla de presupuesto ─────────────────
NEW_BODY = (
    '<div style="margin:0;padding:0;font-size:14px;color:#333;font-family:Arial,sans-serif">'
    '<p style="margin:0 0 16px">Hola <t t-out="object.partner_id.name or \'\'">Cliente</t>,</p>'
    '<p style="margin:0 0 16px">Adjunto te enviamos tu cotización '
    '<strong t-out="object.name or \'\'">S00168</strong> por un total de '
    '<strong t-out="format_amount(object.amount_total, object.currency_id) or \'\'">$ 0</strong>. '
    'En el PDF encontrarás el detalle del producto y las observaciones.</p>'
    '<p style="margin:0 0 16px">Recuerda que la instalación profesional es parte de tu '
    'garantía de 12 meses. Cualquier duda, con gusto te ayudo a elegir la cerradura ideal '
    'para tu puerta.</p>'
    '<p style="margin:0 0 4px">Un saludo,</p>'
    '<p style="margin:0;font-weight:bold;color:#0a0a0a">Sebastián Cabrera</p>'
    '<p style="margin:0;color:#666">Gerente de Operaciones · DigitalSeg</p>'
    '<p style="margin:0;color:#666">+56 9 4688 0196 · digitalseg.cl</p>'
    '</div>'
)

tid = None
data = ex(
    "ir.model.data", "search_read",
    [[["module", "=", "sale"], ["name", "=", "email_template_edi_sale"]]],
    {"fields": ["res_id"], "limit": 1},
)
if data:
    tid = data[0]["res_id"]
if not tid and templates:
    tid = templates[0]["id"]

NEW_SUBJECT = "Tu cotización {{ object.name }} — DigitalSeg"
if tid:
    # El cuerpo y el asunto son traducibles: hay que escribirlos en cada idioma
    # (el correo al cliente se renderiza en es_CL).
    for lang in (None, "es_CL", "es_419", "en_US"):
        ctx = {"lang": lang} if lang else {}
        try:
            ex("mail.template", "write", [[tid], {"body_html": NEW_BODY, "subject": NEW_SUBJECT}], {"context": ctx})
            print(f"  ✅ Plantilla [{tid}] actualizada (lang={lang or 'default'})")
        except Exception as e:
            print(f"  ⚠️ lang={lang}: {e}")
else:
    print("  ⚠️ No se encontró la plantilla de presupuesto")


# ── 3) Subir el correlativo de presupuestos (próximo = 168 → S00168) ─────────
seqs = ex(
    "ir.sequence", "search_read",
    [[["code", "=", "sale.order"]]],
    {"fields": ["id", "name", "number_next_actual", "prefix", "padding"]},
)
for s in seqs:
    cur = s.get("number_next_actual", 0)
    if cur < 168:
        ex("ir.sequence", "write", [[s["id"]], {"number_next": 168}])
        print(f"  ✅ Secuencia [{s['id']}] {s['name']} → próximo número 168 (S00168)")
    else:
        print(f"  ⏭ Secuencia [{s['id']}] ya está en {cur} (no se baja para no duplicar)")
if not seqs:
    print("  ⚠️ No se encontró la secuencia de sale.order")

print("\n✅ Listo. La próxima cotización será S00168, con mensaje humano y firma de Sebastián.")
