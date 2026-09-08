"""
core_client.py — Escribe la VENTA en Zentral Core (CRM) desde el backend.

Zentral Core guarda cada colección como UNA fila jsonb en la tabla `zentral_data`
(user_id, collection, items[]) — mismo patrón que Conta. Registrar una venta es
leer → agregar → upsert de la colección `documentos`.

Se usa para que cada pago aprobado de MercadoPago cree solo su VENTA en el Core
(aparece en «Ventas · Boletas y Facturas», KPIs y Financiero), en paralelo al
ingreso que se registra en Conta. Misma data en ambos lados.

Idempotente por payment_id: el num de la venta es `MP-<payment_id>`, así que un
reintento del webhook no duplica. El srcId del ingreso en Conta se deriva del
MISMO num (`crm:doc:MP-<payment_id>`), de modo que si el CRM llegara a espejar
ese documento a Conta, su propio dedup por srcId evita el doble ingreso.

Config por variables de entorno (Railway) — las MISMAS que conta_client:
  SUPABASE_URL          https://yinsujnsbixfledbpmma.supabase.co
  SUPABASE_SERVICE_KEY  service_role key (NUNCA en el frontend)
  CONTA_OWNER_UID       167fefde-614d-439d-a926-ebae74f1e352  (= sharedOwnerUid del Core)

Falla en silencio (skipped) si no está configurado: nunca rompe el webhook.
"""
from __future__ import annotations

import os
import logging
from typing import Optional

import httpx

log = logging.getLogger("core")

_OWNER_UID_DEFAULT = "167fefde-614d-439d-a926-ebae74f1e352"  # sharedOwnerUid Digitalseg


class CoreClient:
    def __init__(self) -> None:
        self.url = os.getenv("SUPABASE_URL", "").rstrip("/")
        self.key = os.getenv("SUPABASE_SERVICE_KEY", "")
        # El Core comparte dueño con los libros (sharedOwnerUid == CONTA_OWNER_UID).
        self.owner = os.getenv("CONTA_OWNER_UID", _OWNER_UID_DEFAULT)
        self.configured = bool(self.url and self.key and self.owner)
        if not self.configured:
            log.warning("CoreClient no configurado (falta SUPABASE_URL/SUPABASE_SERVICE_KEY) — venta al Core OFF")

    def _headers(self, extra: Optional[dict] = None) -> dict:
        h = {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
        }
        if extra:
            h.update(extra)
        return h

    async def _get_collection(self, client: httpx.AsyncClient, collection: str) -> list:
        r = await client.get(
            f"{self.url}/rest/v1/zentral_data",
            params={
                "user_id": f"eq.{self.owner}",
                "collection": f"eq.{collection}",
                "select": "items",
            },
            headers=self._headers(),
            timeout=15,
        )
        r.raise_for_status()
        rows = r.json()
        if rows and isinstance(rows[0].get("items"), list):
            return rows[0]["items"]
        return []

    async def _upsert_collection(self, client: httpx.AsyncClient, collection: str, items: list) -> None:
        r = await client.post(
            f"{self.url}/rest/v1/zentral_data",
            params={"on_conflict": "user_id,collection"},
            headers=self._headers({"Prefer": "resolution=merge-duplicates,return=minimal"}),
            json={"user_id": self.owner, "collection": collection, "items": items},
            timeout=15,
        )
        r.raise_for_status()

    async def get_stock_map(self) -> dict:
        """{sku: stock} desde la colección `inventario` (solo ítems con campo `sku`).
        Lanza excepción si no puede leer — el llamador decide qué hacer (fail-closed)."""
        if not self.configured:
            raise RuntimeError("core no configurado")
        async with httpx.AsyncClient() as client:
            inv = await self._get_collection(client, "inventario")
        out: dict = {}
        for it in inv:
            sku = str(it.get("sku", "")).strip().lower()
            if not sku:
                continue
            try:
                out[sku] = int(it.get("stock", 0) or 0)
            except (TypeError, ValueError):
                out[sku] = 0
        return out

    async def _descontar_stock(self, client: httpx.AsyncClient, sku_counts: dict) -> dict:
        """Descuenta stock del inventario por SKU (piso 0), en un solo read-modify-write.
        Best-effort: el llamador NO debe romper la venta si esto falla."""
        if not sku_counts:
            return {"skipped": True, "reason": "sin skus"}
        inv = await self._get_collection(client, "inventario")
        changed: dict = {}
        for it in inv:
            sku = str(it.get("sku", "")).strip().lower()
            if sku not in sku_counts:
                continue
            try:
                cur = int(it.get("stock", 0) or 0)
            except (TypeError, ValueError):
                cur = 0
            new = max(0, cur - int(sku_counts[sku]))
            it["stock"] = new
            it["estado"] = "low" if new <= 1 else "ok"
            changed[sku] = {"antes": cur, "ahora": new}
        if changed:
            await self._upsert_collection(client, "inventario", inv)
        return {"ok": True, "descontado": changed}

    async def registrar_venta_mp(self, payment: dict) -> dict:
        """Crea la venta en el Core (documentos) desde un pago MP aprobado.
        Idempotente por num = MP-<payment_id>. No duplica en reintentos.
        Al crear la venta (no en dedup) descuenta stock por SKU (best-effort)."""
        if not self.configured:
            return {"skipped": True, "reason": "core no configurado"}

        payment_id = str(payment.get("id", ""))
        if not payment_id:
            return {"skipped": True, "reason": "sin payment id"}

        num = f"MP-{payment_id}"
        total = round(float(payment.get("transaction_amount", 0) or 0))
        if total <= 0:
            return {"skipped": True, "reason": "monto 0"}

        fees = payment.get("fee_details") or []
        comision = round(sum(float(f.get("amount", 0) or 0) for f in fees))
        cuotas = int(payment.get("installments", 1) or 1)
        fecha = str(payment.get("date_approved") or payment.get("date_created") or "")[:10]

        meta = payment.get("metadata") or {}
        payer = payment.get("payer") or {}
        # Pago por link web trae metadata.cliente; el POS (maquinita) no → cae a payer o genérico.
        es_pos = not (meta.get("cliente") or meta.get("lead_id"))
        cliente = (
            meta.get("cliente")
            or (f"{payer.get('first_name','')} {payer.get('last_name','')}".strip())
            or payer.get("email")
            or ("Cliente Mercado Pago (POS)" if es_pos else "Cliente Mercado Pago")
        )
        producto = meta.get("producto") or payment.get("description") or ("Venta POS Mercado Pago" if es_pos else "Compra Mercado Pago")
        ext_ref = payment.get("external_reference", "") or ""

        try:
            async with httpx.AsyncClient() as client:
                docs = await self._get_collection(client, "documentos")
                if any(str(d.get("num", "")) == num or str(d.get("paymentId", "")) == payment_id for d in docs):
                    return {"ok": True, "dedup": True, "num": num}

                doc = {
                    "num": num,
                    "tipo": "Boleta",
                    "cliente": cliente,
                    "rut": "",
                    "producto": producto,
                    "qty": 1,
                    "total": total,
                    "medioPago": "MercadoPago",
                    "cuotas": cuotas if cuotas > 1 else None,
                    "comisionMP": comision,
                    "fecha": fecha,
                    "estado": "por emitir",          # dinero recibido; la boleta/DTE se emite aparte
                    "origen": "mercadopago",
                    "canal": "pos" if es_pos else "web",
                    "extRef": ext_ref,
                    "paymentId": payment_id,
                    "_contaEspejado": True,          # el backend ya registró el ingreso en Conta → el CRM no re-espeja
                }
                docs.insert(0, doc)
                await self._upsert_collection(client, "documentos", docs)

                # Descontar stock por SKU (solo ventas WEB: metadata.skus). Best-effort:
                # si falla, la venta YA quedó guardada; se registra el aviso y no se rompe.
                stock_res = None
                skus_raw = str(meta.get("skus", "") or "")
                if skus_raw:
                    sku_counts: dict = {}
                    for s in skus_raw.split(","):
                        s = s.strip().lower()
                        if s:
                            sku_counts[s] = sku_counts.get(s, 0) + 1
                    try:
                        stock_res = await self._descontar_stock(client, sku_counts)
                        log.info("Core: stock descontado %s", stock_res.get("descontado"))
                    except Exception as exc:
                        log.warning("Core: descuento de stock falló (venta OK): %s", exc)
                        stock_res = {"ok": False, "error": str(exc)}
        except httpx.HTTPStatusError as exc:
            log.error("Core venta MP HTTP %s: %s", exc.response.status_code, exc.response.text[:300])
            return {"ok": False, "error": "http", "status": exc.response.status_code}
        except Exception as exc:
            log.error("Core venta MP error: %s", exc)
            return {"ok": False, "error": str(exc)}

        log.info("Core: venta MP registrada num=%s total=%d canal=%s", num, total, "pos" if es_pos else "web")
        return {"ok": True, "num": num, "total": total, "canal": "pos" if es_pos else "web", "stock": stock_res}
