from __future__ import annotations
import os
import xmlrpc.client
import httpx
from functools import cached_property
from typing import Any, Optional
from html import escape


class OdooClient:
    def __init__(self) -> None:
        self.url            = os.environ["ODOO_URL"]
        self.db             = os.environ["ODOO_DB"]
        self.user           = os.environ["ODOO_USER"]
        self.apikey         = os.environ["ODOO_APIKEY"]
        self.source_id      = int(os.getenv("ODOO_SOURCE", "13"))
        self.salesperson_id = int(os.getenv("ODOO_SALESPERSON_ID", "6"))  # Sebastian Cabrera
        self._uid: Optional[int] = None
        # Si ODOO_PROXY_URL está definido, todas las llamadas van por el proxy PHP
        self._proxy_url    = os.getenv("ODOO_PROXY_URL", "")
        self._proxy_secret = os.getenv("PROXY_SECRET", "")

    @cached_property
    def _common(self) -> xmlrpc.client.ServerProxy:
        return xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common")

    @cached_property
    def _models(self) -> xmlrpc.client.ServerProxy:
        return xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/object")

    @property
    def uid(self) -> int:
        if self._uid is None:
            if self._proxy_url:
                self._uid = 2  # el proxy maneja auth internamente
            else:
                self._uid = self._common.authenticate(self.db, self.user, self.apikey, {})
                if not self._uid:
                    raise RuntimeError("Odoo authentication failed")
        return self._uid

    def _exec(self, model: str, method: str, args: list, kwargs: dict = {}) -> Any:
        if self._proxy_url:
            return self._exec_via_proxy(model, method, args, kwargs)
        return self._models.execute_kw(
            self.db, self.uid, self.apikey, model, method, args, kwargs
        )

    def _exec_via_proxy(self, model: str, method: str, args: list, kwargs: dict) -> Any:
        resp = httpx.post(
            self._proxy_url,
            headers={"X-Proxy-Secret": self._proxy_secret},
            json={"model": model, "method": method, "args": args, "kwargs": kwargs},
            timeout=25,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("error", "Proxy error"))
        return data["result"]

    # ── Partners ────────────────────────────────────────────────────────────────

    def find_or_create_partner(
        self,
        name: str,
        phone: str,
        city: Optional[str] = None,
        company_name: Optional[str] = None,
        vat: Optional[str] = None,
        email: Optional[str] = None,
    ) -> int:
        ids = self._exec(
            "res.partner", "search",
            [[["phone", "=", phone]]],
        )
        if ids:
            partner_id = ids[0]
            # Si el contacto existe pero no tiene email, lo completamos
            # (necesario para que Odoo pueda enviarle la cotización).
            if email:
                try:
                    data = self._exec("res.partner", "read", [[partner_id]], {"fields": ["email"]})
                    if data and not data[0].get("email"):
                        self._exec("res.partner", "write", [[partner_id], {"email": email}])
                except Exception:
                    pass
            return partner_id

        vals: dict[str, Any] = {
            "name": name,
            "phone": phone,
            "customer_rank": 1,
        }
        if city:
            vals["city"] = city
        # 'company_name' NO es un campo válido en res.partner (Odoo 19): lo omitimos
        # a propósito. La razón social viaja en el lead/WhatsApp para el equipo.
        if company_name:
            vals["comment"] = f"Razón social: {company_name}"
        if vat:
            vals["vat"] = vat
        if email:
            vals["email"] = email

        try:
            return self._exec("res.partner", "create", [vals])
        except Exception:
            # Defensa: si algún campo opcional es rechazado por esta versión de Odoo,
            # reintentamos con lo mínimo para NO perder NUNCA el lead.
            minimal: dict[str, Any] = {"name": name, "phone": phone, "customer_rank": 1}
            if email:
                minimal["email"] = email
            return self._exec("res.partner", "create", [minimal])

    # ── Products ────────────────────────────────────────────────────────────────

    def find_product(self, sku: str) -> Optional[int]:
        ids = self._exec(
            "product.product", "search",
            [[["default_code", "=", sku]]],
        )
        return ids[0] if ids else None

    def check_stock(self, sku: str) -> float:
        ids = self._exec("product.product", "search", [[["default_code", "=", sku]]])
        if not ids:
            return 0.0
        data = self._exec("product.product", "read", [ids[:1]], {"fields": ["qty_available"]})
        return float(data[0].get("qty_available", 0)) if data else 0.0

    # ── CRM Leads ───────────────────────────────────────────────────────────────

    def create_lead(
        self,
        partner_id: int,
        phone: str,
        product_name: str,
        sku: str,
        price: float,
        requirements: dict,
        source_label: str,
        quantity: int = 1,
        needs_gateway: bool = False,
        email: Optional[str] = None,
    ) -> int:
        features = escape(", ".join(str(f) for f in requirements.get("features", [])))
        desc = (
            f"<p><b>Producto:</b> {escape(str(product_name))} (SKU: {escape(str(sku))}) — ${price:,.0f}</p>"
            f"<p><b>Espacio:</b> {escape(str(requirements.get('space', '')))} | "
            f"<b>Puerta:</b> {escape(str(requirements.get('doorType', '')))} | "
            f"<b>Grosor:</b> {escape(str(requirements.get('thickness', '')))} mm</p>"
            f"<p><b>Funciones:</b> {features}</p>"
            f"<p><b>Gateway adicional:</b> {'Sí' if needs_gateway else 'No'}</p>"
            f"<p><b>Cantidad:</b> {quantity}</p>"
            f"<p><b>Origen:</b> {escape(str(source_label))}</p>"
        )

        vals: dict[str, Any] = {
            "name": f"Solicitud cotizador — {product_name}",
            "partner_id": partner_id,
            "phone": phone,
            "type": "opportunity",
            "source_id": self.source_id,
            "description": desc,
            "user_id": self.salesperson_id,
            "expected_revenue": price * quantity,
        }
        if email:
            vals["email_from"] = email
        return self._exec("crm.lead", "create", [vals])

    # ── Sale Orders ─────────────────────────────────────────────────────────────

    def create_sale_order(
        self,
        partner_id: int,
        product_id: int,
        product_name: str,
        price: float,
        quantity: int,
        commitment_date: Optional[str] = None,
        note: str = "",
        install_price: float = 0,
        install_label: str = "",
    ) -> int:
        order_vals: dict[str, Any] = {
            "partner_id": partner_id,
            "state": "draft",
            "user_id": self.salesperson_id,
        }
        if commitment_date:
            order_vals["commitment_date"] = commitment_date
        if note:
            order_vals["note"] = note

        order_id = self._exec("sale.order", "create", [order_vals])

        self._exec("sale.order.line", "create", [{
            "order_id": order_id,
            "product_id": product_id,
            "name": product_name,
            "product_uom_qty": quantity,
            "price_unit": price,
        }])

        # Línea de instalación profesional (obligatoria — parte de la garantía)
        if install_price and install_price > 0:
            try:
                self._exec("sale.order.line", "create", [{
                    "order_id": order_id,
                    "product_id": self._install_product_id(),
                    "name": install_label or "Instalación profesional",
                    "product_uom_qty": 1,
                    "price_unit": install_price,
                }])
            except Exception:
                pass

        return order_id

    def _install_product_id(self) -> int:
        """Devuelve (o crea una vez) el producto de servicio 'Instalación profesional'."""
        cached = getattr(self, "_inst_pid", None)
        if cached:
            return cached
        ids = self._exec("product.product", "search", [[["default_code", "=", "INST-DIGITALSEG"]]])
        if ids:
            self._inst_pid = ids[0]
        else:
            self._inst_pid = self._exec("product.product", "create", [{
                "name": "Instalación profesional DigitalSeg",
                "default_code": "INST-DIGITALSEG",
                "type": "service",
                "list_price": 89990,
                "sale_ok": True,
                "purchase_ok": False,
            }])
        return self._inst_pid

    def send_quotation_email(self, order_id: int) -> bool:
        """Envía el presupuesto oficial de Odoo por correo al cliente del pedido."""
        template_id = None
        # 1) Plantilla estándar de presupuesto por external id (robusto al idioma).
        try:
            data = self._exec(
                "ir.model.data", "search_read",
                [[["module", "=", "sale"], ["name", "=", "email_template_edi_sale"]]],
                {"fields": ["res_id"], "limit": 1},
            )
            if data:
                template_id = data[0]["res_id"]
        except Exception:
            template_id = None
        # 2) Fallback: cualquier plantilla de correo de sale.order.
        if not template_id:
            ids = self._exec(
                "mail.template", "search",
                [[["model", "=", "sale.order"]]], {"limit": 1},
            )
            if ids:
                template_id = ids[0]
        if not template_id:
            return False
        self._exec(
            "mail.template", "send_mail",
            [[template_id], order_id], {"force_send": True},
        )
        # Marcar la cotización como "enviada" en Odoo (si sigue en borrador).
        try:
            self._exec("sale.order", "write", [[order_id], {"state": "sent"}])
        except Exception:
            pass
        return True

    def send_html_email(
        self,
        subject: str,
        body_html: str,
        to_addresses: list[str],
        email_from: str = "",
    ) -> int:
        """Envía un correo HTML propio usando el servidor de correo saliente de
        Odoo (que ya funciona para digitalseg.cl con SPF/DKIM). Crea un
        mail.mail y lo despacha con force_send."""
        vals: dict[str, Any] = {
            "subject": subject,
            "body_html": body_html,
            "email_to": ",".join(to_addresses),
            "auto_delete": True,
        }
        if email_from:
            vals["email_from"] = email_from
        mail_id = self._exec("mail.mail", "create", [vals])
        try:
            self._exec("mail.mail", "send", [[mail_id]])
        except xmlrpc.client.Fault as e:
            # Odoo saas-19.2: mail.mail.send() devuelve None y el marshaller XML-RPC
            # del servidor (allow_none=False) lanza Fault AL SERIALIZAR la respuesta.
            # El envío YA se ejecutó en el servidor, así que lo tratamos como éxito.
            if "marshal None" in str(e) or "allow_none" in str(e):
                pass
            else:
                raise
        return mail_id

    # ── Calendar events ──────────────────────────────────────────────────────────

    def create_calendar_event(
        self,
        name: str,
        start_dt: str,       # "2026-05-12 10:00:00" UTC
        stop_dt: str,
        description: str = "",
        location: str = "",
        partner_id: Optional[int] = None,
    ) -> int:
        vals: dict[str, Any] = {
            "name": name,
            "start": start_dt,
            "stop": stop_dt,
            "description": description,
            "location": location,
        }
        if partner_id:
            vals["partner_ids"] = [(4, partner_id)]
        return self._exec("calendar.event", "create", [vals])

    # ── URL helpers ─────────────────────────────────────────────────────────────

    def lead_url(self, lead_id: int) -> str:
        return f"{self.url}/odoo/crm/{lead_id}"

    def sale_url(self, order_id: int) -> str:
        return f"{self.url}/odoo/sales/{order_id}"
