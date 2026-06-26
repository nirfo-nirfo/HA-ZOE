import hashlib
import hmac
from typing import Any

import httpx

from app.logging_config import logger
from app.settings import settings

GRAPH_API_BASE = "https://graph.facebook.com/v20.0"


def verify_signature(raw_body: bytes, signature_header: str | None) -> bool:
    """Validates Meta's X-Hub-Signature-256 header against the app secret."""
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(
        settings.whatsapp_app_secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    received = signature_header.removeprefix("sha256=")
    return hmac.compare_digest(expected, received)


def extract_text_message(payload: dict[str, Any]) -> tuple[str, str] | None:
    """Pulls (sender_number, text) out of a WhatsApp webhook payload, or None."""
    try:
        entry = payload["entry"][0]
        change = entry["changes"][0]["value"]
        messages = change.get("messages")
        if not messages:
            return None
        message = messages[0]
        if message.get("type") != "text":
            return None
        sender = message["from"]
        text = message["text"]["body"]
        return sender, text
    except (KeyError, IndexError, TypeError):
        logger.warning("Could not parse WhatsApp webhook payload: %s", payload)
        return None


async def send_message(to: str, text: str) -> None:
    url = f"{GRAPH_API_BASE}/{settings.whatsapp_phone_number_id}/messages"
    headers = {"Authorization": f"Bearer {settings.whatsapp_access_token}"}
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text},
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code >= 300:
            logger.error("WhatsApp send failed: HTTP %s %s", resp.status_code, resp.text)
    except httpx.HTTPError as exc:
        logger.error("WhatsApp send failed: %s", exc)
