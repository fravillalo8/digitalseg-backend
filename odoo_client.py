from __future__ import annotations
import os
import xmlrpc.client
from functools import cached_property
from typing import Any, Optional


class OdooClient:
    def __init__(self) -> None:
        self.url    = os.environ["ODOO_URL"]
        self.db     = os.environ["ODOO_DB"]
        self.user   = os.environ["ODOO_USER"]
        self.apikey = os.environ["ODOO_APIKEY"]
        self.source_id = int(os.getenv("ODOO_SOURCE", "13"))
        self._uid: Optional[int] = None

    @cached_property
    def _common(self) -> xmlrpc.client.ServerProxy:
        return xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common")

    @cached_property
    def _models(self) -> xmlrpc.client.ServerProxy:
        return xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/object")

    @property
    def uid(self) -> int:
        if self._uid is None:
            self._uid = self._common.authenticate(self.db, self.user, self.apikey, {})
            if not self._uid:
                raise RuntimeError("Odoo authentication failed")
        return self._uid

    def _exec(self, model: str, method: str, args: list, kwargs: dict = {}) -> Any:
        return self._models.execute_kw(
            self.db, self.uid, self.apikey, model, method, args, kwargs
        )

    # ── Partners ────────────────────────────────────────────────────────────────

    def find_or_create_partner(
        self,
        name: str,
        phone: str,
        city: Optional[str] = None,
        company_name: Optional[str] = None,
        vat: Optional[str] = None,
    ) -> int:
        ids = self._exec(
            "res.partner", "search",
            [[["phone", "=", phone]]],
        )
        if ids:
            return ids[0]

        vals: dict[str, Any] = {
            "name": name,
            "phone": phone,
            "customer_rank": 1,
        }
        if city:
            vals["city"] = city
        if company_name:
            vals["company_name"] = company_name
        if vat:
            vals["vat"] = vat

        return self._exec("res.partner", "create", [vals])

    # ── Products ────────────────────────────────────────────────────────────────

    def find_product(self, sku: str) -> Optional[int]:
        ids = self._exec(
            "product.product", "search",
            [[["default_code", "=", sku]]],
        )
        return ids[0] if ids else None

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
    ) -> int:
        features = ", ".join(requirements.get("features", []))
        desc = (
            f"<p><b>Producto:</b> {product_name} (SKU: {sku}) — ${price:,.0f}</p>"
            f"<p><b>Espacio:</b> {requirements.get('space', '')} | "
            f"<b>Puerta:</b> {requirements.get('doorType', '')} | "
            f"<b>Grosor:</b> {requirements.get('thickness', '')} mm</p>"
            f"<p><b>Funciones:</b> {features}</p>"
            f"<p><b>Gateway adicional:</b> {'Sí' if needs_gateway else 'No'}</p>"
            f"<p><b>Cantidad:</b> {quantity}</p>"
            f"<p><b>Origen:</b> {source_label}</p>"
        )

        vals: dict[str, Any] = {
            "name": f"Solicitud cotizador — {product_name}",
            "partner_id": partner_id,
            "phone": phone,
            "type": "opportunity",
            "source_id": self.source_id,
            "description": desc,
        }
        return self._exec("crm.lead", "create", [vals])

    # ── Sale Orders ─────────────────────────────────────────────────────────────

    def create_sale_order(
        self,
        partner_id: int,
        product_id: int,
        product_name: str,
        price: float,
        quantity: int,
    ) -> int:
        order_id = self._exec("sale.order", "create", [{
            "partner_id": partner_id,
            "state": "draft",
        }])

        self._exec("sale.order.line", "create", [{
            "order_id": order_id,
            "product_id": product_id,
            "name": product_name,
            "product_uom_qty": quantity,
            "price_unit": price,
        }])

        return order_id

    # ── URL helpers ─────────────────────────────────────────────────────────────

    def lead_url(self, lead_id: int) -> str:
        return f"{self.url}/odoo/crm/{lead_id}"

    def sale_url(self, order_id: int) -> str:
        return f"{self.url}/odoo/sales/{order_id}"
