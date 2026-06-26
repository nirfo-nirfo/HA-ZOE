from fastapi import FastAPI, Request, Response

from app.claude_agent import decide_action, get_known_entities
from app.confirmation import make_pending, pop_if_confirmed, store_pending
from app.ha_client import ha_client
from app.logging_config import logger
from app.settings import settings
from app.whatsapp import extract_text_message, send_message, verify_signature

app = FastAPI(title="ZOE")


@app.get("/webhook")
async def verify_webhook(request: Request) -> Response:
    params = request.query_params
    if (
        params.get("hub.mode") == "subscribe"
        and params.get("hub.verify_token") == settings.whatsapp_verify_token
    ):
        return Response(content=params.get("hub.challenge", ""), media_type="text/plain")
    return Response(status_code=403)


@app.post("/webhook")
async def receive_webhook(request: Request) -> Response:
    raw_body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")
    if not verify_signature(raw_body, signature):
        logger.warning("Rejected webhook with invalid signature")
        return Response(status_code=403)

    payload = await request.json()
    parsed = extract_text_message(payload)
    if parsed is None:
        return Response(status_code=200)

    sender, text = parsed
    if sender != settings.allowed_sender_number:
        logger.warning("Rejected message from unauthorized sender %s", sender)
        return Response(status_code=200)

    logger.info("Inbound from %s: %s", sender, text)
    await _handle_message(sender, text)
    return Response(status_code=200)


async def _handle_message(sender: str, text: str) -> None:
    confirmed = pop_if_confirmed(sender, text)
    if confirmed is not None:
        logger.info("Confirmed risky action: %s", confirmed.description)
        success, detail = await ha_client.call_service(
            confirmed.domain, confirmed.service, confirmed.entity_id, confirmed.service_data
        )
        if success:
            await send_message(sender, f"{confirmed.description} ✅")
        else:
            await send_message(sender, f"Failed: {confirmed.description} — {detail}")
        return

    known_entities = get_known_entities()
    states = await ha_client.get_states(list(known_entities.keys()))

    action = decide_action(text, states)
    if action is None:
        await send_message(sender, "I'm not sure what you mean — could you rephrase?")
        return

    entity_id = action["entity_id"]
    entity_def = known_entities.get(entity_id)
    if entity_def is None:
        logger.error("Claude returned unknown entity_id: %s", entity_id)
        await send_message(sender, "I tried to act on a device I don't recognize. Ignored for safety.")
        return

    domain = action["domain"]
    service = action["service"]
    service_data = action.get("service_data") or {}
    description = f"{entity_def['name']}: {service}"

    if entity_def.get("risky"):
        pending = make_pending(entity_id, domain, service, service_data, description)
        store_pending(sender, pending)
        logger.info("Risky action pending confirmation: %s", description)
        await send_message(
            sender, f"This will: {description}. Reply 'yes' to confirm."
        )
        return

    logger.info("Executing: %s", description)
    success, detail = await ha_client.call_service(domain, service, entity_id, service_data)
    if success:
        await send_message(sender, f"{description} ✅")
    else:
        await send_message(sender, f"Failed: {description} — {detail}")
