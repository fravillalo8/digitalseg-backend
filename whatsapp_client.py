from __future__ import annotations
import os
import hmac
import hashlib
import logging
import httpx
from typing import Any, Optional

log = logging.getLogger(__name__)

GRAPH_URL = "https://graph.facebook.com/v21.0"


class WhatsAppClient:
    def __init__(self) -> None:
        self.phone_number_id = os.getenv("META_PHONE_NUMBER_ID", "")
        self.access_token    = os.getenv("META_ACCESS_TOKEN", "")
        self.app_secret      = os.getenv("META_APP_SECRET", "")
        self._configured     = bool(self.phone_number_id and self.access_token)

    # ── Firma ────────────────────────────────────────────────────────────────────

    def verify_signature(self, payload_bytes: bytes, signature_header: str) -> bool:
        """Valida X-Hub-Signature-256: sha256=<hex>"""
        if not self.app_secret:
            log.warning("META_APP_SECRET no configurado — saltando validación de firma")
            return True
        expected = hmac.new(
            self.app_secret.encode(), payload_bytes, hashlib.sha256
        ).hexdigest()
        received = signature_header.removeprefix("sha256=")
        return hmac.compare_digest(expected, received)

    # ── Envío de plantillas ──────────────────────────────────────────────────────

    def send_template(
        self,
        to: str,
        template_name: str,
        language: str = "es_AR",
        components: Optional[list[dict]] = None,
    ) -> dict:
        if not self._configured:
            log.warning("WhatsApp no configurado — template '%s' no enviado", template_name)
            return {"skipped": True, "reason": "META_PHONE_NUMBER_ID o META_ACCESS_TOKEN no definidos"}

        body: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": language},
            },
        }
        if components:
            body["template"]["components"] = components

        resp = httpx.post(
            f"{GRAPH_URL}/{self.phone_number_id}/messages",
            headers={"Authorization": f"Bearer {self.access_token}"},
            json=body,
            timeout=10,
        )
        resp.raise_for_status()
        log.info("Template '%s' enviado a %s → %s", template_name, to, resp.json())
        return resp.json()

    # ── Helpers de plantillas específicas ────────────────────────────────────────

    def solicitud_recibida(
        self, to: str, nombre: str, producto: str, puerta: str, ciudad: str
    ) -> dict:
        """
        solicitud_cotizacion_recibida
        {{1}} nombre  {{2}} producto  {{3}} tipo puerta  {{4}} ciudad
        """
        return self.send_template(
            to=to,
            template_name="solicitud_cotizacion_recibida",
            components=[{
                "type": "body",
                "parameters": [
                    {"type": "text", "text": nombre},
                    {"type": "text", "text": producto},
                    {"type": "text", "text": puerta},
                    {"type": "text", "text": ciudad or "tu ciudad"},
                ],
            }],
        )

    def visita_agendada(self, to: str, nombre: str, fecha_hora: str) -> dict:
        """
        visita_tecnica_agendada
        {{1}} nombre  {{2}} fecha y hora
        """
        return self.send_template(
            to=to,
            template_name="visita_tecnica_agendada",
            components=[{
                "type": "body",
                "parameters": [
                    {"type": "text", "text": nombre},
                    {"type": "text", "text": fecha_hora},
                ],
            }],
        )

    def cotizacion_lista(
        self, to: str, nombre: str, producto: str, total: str
    ) -> dict:
        """
        cotizacion_formal_lista
        {{1}} nombre  {{2}} producto  {{3}} total
        """
        return self.send_template(
            to=to,
            template_name="cotizacion_formal_lista",
            components=[{
                "type": "body",
                "parameters": [
                    {"type": "text", "text": nombre},
                    {"type": "text", "text": producto},
                    {"type": "text", "text": total},
                ],
            }],
        )

    # ── Parser de mensajes entrantes ─────────────────────────────────────────────

    @staticmethod
    def parse_incoming(body: dict) -> list[dict]:
        """
        Extrae lista de mensajes del payload de webhook de Meta.
        Cada item: {from, name, type, text, message_id, timestamp}
        """
        messages = []
        for entry in body.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                contacts = {c["wa_id"]: c["profile"]["name"] for c in value.get("contacts", [])}
                for msg in value.get("messages", []):
                    messages.append({
                        "from":       msg["from"],
                        "name":       contacts.get(msg["from"], ""),
                        "type":       msg.get("type", ""),
                        "text":       msg.get("text", {}).get("body", ""),
                        "message_id": msg.get("id", ""),
                        "timestamp":  msg.get("timestamp", ""),
                    })
        return messages
