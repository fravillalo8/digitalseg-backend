"""
conta_client.py — Escribe movimientos en Zentral Conta (Supabase) desde el backend.

Zentral Conta guarda cada colección como UNA fila jsonb en la tabla `conta_data`
(user_id, collection, items[]). Por eso registrar un movimiento es leer → agregar → upsert.

Se usa para que los pagos de MercadoPago actualicen la contabilidad SOLOS:
cuando un pago se aprueba, el webhook registra la COMISIÓN REAL que cobró MercadoPago
(desde fee_details) como un egreso en Conta. Como Zentral espeja Conta (contaSync),
el Informe contable se actualiza sin intervención.

Config por variables de entorno (Railway):
  SUPABASE_URL          https://yinsujnsbixfledbpmma.supabase.co
  SUPABASE_SERVICE_KEY  service_role key (NUNCA en el frontend)
  CONTA_OWNER_UID       167fefde-614d-439d-a926-ebae74f1e352  (dueño de los libros Digitalseg)

Falla en silencio (skipped) si no está configurado: nunca rompe el webhook.
"""
from __future__ import annotations

import os
import logging
from typing import Optional

import httpx

log = logging.getLogger("conta")

_OWNER_UID_DEFAULT = "167fefde-614d-439d-a926-ebae74f1e352"  # francisco.villalobos@digitalseg.cl
_CAT_COMISION = "Comisiones de pago (MercadoPago)"


class ContaClient:
    def __init__(self) -> None:
        self.url = os.getenv("SUPABASE_URL", "").rstrip("/")
        self.key = os.getenv("SUPABASE_SERVICE_KEY", "")
        self.owner = os.getenv("CONTA_OWNER_UID", _OWNER_UID_DEFAULT)
        self.configured = bool(self.url and self.key and self.owner)
        if not self.configured:
            log.warning("ContaClient no configurado (falta SUPABASE_URL/SUPABASE_SERVICE_KEY) — registro contable OFF")

    # ── REST helpers (PostgREST con service_role) ────────────────────────────
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
            f"{self.url}/rest/v1/conta_data",
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
            f"{self.url}/rest/v1/conta_data",
            params={"on_conflict": "user_id,collection"},
            headers=self._headers({"Prefer": "resolution=merge-duplicates,return=minimal"}),
            json={"user_id": self.owner, "collection": collection, "items": items},
            timeout=15,
        )
        r.raise_for_status()

    # ── Registro de la comisión de MercadoPago ───────────────────────────────
    async def registrar_comision_mp(self, payment: dict) -> dict:
        """Agrega un egreso con la comisión REAL de MercadoPago del pago aprobado.
        Idempotente: no duplica si ya existe un movimiento con este paymentId."""
        if not self.configured:
            return {"skipped": True, "reason": "conta no configurado"}

        payment_id = str(payment.get("id", ""))
        if not payment_id:
            return {"skipped": True, "reason": "sin payment id"}

        # Comisión real = suma de fee_details (mercadopago_fee + financing_fee de las cuotas)
        fees = payment.get("fee_details") or []
        comision = round(sum(float(f.get("amount", 0) or 0) for f in fees))
        if comision <= 0:
            return {"skipped": True, "reason": "sin comisión (fee_details vacío)"}

        cuotas = int(payment.get("installments", 1) or 1)
        fecha = str(payment.get("date_approved") or payment.get("date_created") or "")[:10]
        transaction = round(float(payment.get("transaction_amount", 0) or 0))
        neto_recibido = payment.get("transaction_details", {}).get("net_received_amount")

        # La comisión MP es afecta a IVA (MP emite factura): separo neto/IVA
        neto = round(comision / 1.19)
        iva = comision - neto

        try:
            async with httpx.AsyncClient() as client:
                empresas = await self._get_collection(client, "empresas")
                empresa_id = empresas[0]["id"] if empresas and empresas[0].get("id") else ""

                movs = await self._get_collection(client, "movimientos")
                if any(str(m.get("paymentId", "")) == payment_id for m in movs):
                    return {"ok": True, "dedup": True, "payment_id": payment_id}

                mov = {
                    "id": f"mov_mp_{payment_id}",
                    "empresaId": empresa_id,
                    "tipo": "egreso",
                    "fecha": fecha,
                    "categoria": _CAT_COMISION,
                    "glosa": f"Comisión MercadoPago pago #{payment_id}"
                             + (f" · {cuotas} cuotas" if cuotas > 1 else "")
                             + (f" · venta ${transaction:,}".replace(",", ".") if transaction else ""),
                    "contraparte": "MercadoPago",
                    "neto": neto,
                    "iva": iva,
                    "total": comision,
                    "afectoIva": True,
                    "docTipo": "Factura",
                    "docFolio": "",
                    "estado": "pagado",
                    "origen": "mercadopago",
                    "paymentId": payment_id,
                }
                movs.insert(0, mov)
                await self._upsert_collection(client, "movimientos", movs)
        except httpx.HTTPStatusError as exc:
            log.error("Conta comisión MP HTTP %s: %s", exc.response.status_code, exc.response.text[:300])
            return {"ok": False, "error": "http", "status": exc.response.status_code}
        except Exception as exc:
            log.error("Conta comisión MP error: %s", exc)
            return {"ok": False, "error": str(exc)}

        log.info("Conta: comisión MP registrada pago=%s comisión=%d neto_recibido=%s", payment_id, comision, neto_recibido)
        return {"ok": True, "payment_id": payment_id, "comision": comision}
