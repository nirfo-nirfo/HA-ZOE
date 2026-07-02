import asyncio
from datetime import datetime

from fastapi import FastAPI, Request, Response

from app.claude_agent import REMINDER_TOOLS, decide_actions, get_known_entities
from app.confirmation import make_pending, pop_if_confirmed, store_pending
from app.ha_client import ha_client
from app.logging_config import logger
from app.reminders import add_reminder, delete_reminder, list_reminders, pop_due
from app.settings import settings
from app.whatsapp import extract_text_message, send_message, verify_signature

app = FastAPI(title="ZOE")


@app.on_event("startup")
async def startup() -> None:
    asyncio.create_task(_reminder_loop())


async def _reminder_loop() -> None:
    while True:
        await asyncio.sleep(60)
        for reminder in pop_due():
            logger.info("Firing reminder %s for %s", reminder.id, reminder.sender)
            await send_message(reminder.sender, f"⏰ {reminder.text}")


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
    allowed = {n.strip() for n in settings.allowed_sender_numbers.split(",")}
    if sender not in allowed:
        logger.warning("Rejected message from unauthorized sender %s", sender)
        return Response(status_code=200)

    logger.info("Inbound from %s: %s", sender, text)
    await _handle_message(sender, text)
    return Response(status_code=200)


async def _execute_control_action(entity_id: str, domain: str, service: str, service_data: dict, description: str) -> str:
    logger.info("Executing: %s", description)
    success, detail = await ha_client.call_service(domain, service, entity_id, service_data)
    if success:
        return f"{description} ✅"
    return f"Failed: {description} — {detail}"


async def _auto_turn_off_later(sender: str, entity_id: str, domain: str, name: str, minutes: float) -> None:
    await asyncio.sleep(minutes * 60)
    logger.info("Auto turn-off firing for %s after %s minutes", entity_id, minutes)
    success, detail = await ha_client.call_service(domain, "turn_off", entity_id, {})
    if success:
        await send_message(sender, f"{name}: turned off automatically after {minutes:g} min ✅")
    else:
        await send_message(sender, f"{name}: failed to auto turn-off — {detail}")


def _handle_reminder_call(sender: str, tool: str, inp: dict) -> str:
    if tool == "set_reminder":
        try:
            send_at = datetime.fromisoformat(inp["send_at"]).timestamp()
        except (ValueError, KeyError):
            return "I couldn't parse that date/time — please try again."
        reminder = add_reminder(sender, inp["text"], send_at)
        when = datetime.fromtimestamp(reminder.send_at).strftime("%d/%m/%Y %H:%M")
        return f"Reminder set ✅ — I'll message you on {when}: {reminder.text}"

    if tool == "list_reminders":
        pending = list_reminders(sender)
        if not pending:
            return "You have no pending reminders."
        lines = [
            f"• [{r.id}] {datetime.fromtimestamp(r.send_at).strftime('%d/%m %H:%M')} — {r.text}"
            for r in pending
        ]
        return "Your reminders:\n" + "\n".join(lines)

    if tool == "delete_reminder":
        rid = inp.get("id", "")
        if delete_reminder(rid, sender):
            return f"Reminder {rid} deleted ✅"
        return f"Reminder {rid} not found."

    return ""


async def _handle_message(sender: str, text: str) -> None:
    confirmed = pop_if_confirmed(sender, text)
    if confirmed is not None:
        replies = []
        for action in confirmed:
            logger.info("Confirmed risky action: %s", action.description)
            replies.append(
                await _execute_control_action(
                    action.entity_id, action.domain, action.service, action.service_data, action.description
                )
            )
        await send_message(sender, "\n".join(replies))
        return

    known_entities = get_known_entities()
    states = await ha_client.get_states(list(known_entities.keys()))

    tool_calls, assistant_text = decide_actions(text, states)
    if not tool_calls:
        await send_message(sender, assistant_text or "I'm not sure what you mean — could you rephrase?")
        return

    immediate_replies: list[str] = []
    pending_actions = []

    for call in tool_calls:
        tool = call["tool"]
        inp = call["input"]

        if tool in REMINDER_TOOLS:
            immediate_replies.append(_handle_reminder_call(sender, tool, inp))
            continue

        entity_id = inp.get("entity_id")
        entity_def = known_entities.get(entity_id)
        if entity_def is None:
            logger.error("Claude returned unknown entity_id: %s", entity_id)
            immediate_replies.append("I tried to act on a device I don't recognize. Ignored for safety.")
            continue

        if tool == "get_device_status":
            state = states.get(entity_id, {}).get("state", "unknown")
            immediate_replies.append(f"{entity_def['name']}: {state}")
            continue

        domain = inp["domain"]
        service = inp["service"]
        service_data = inp.get("service_data") or {}
        duration_minutes = inp.get("duration_minutes")
        description = f"{entity_def['name']}: {service}"

        if entity_def.get("risky"):
            pending_actions.append(make_pending(entity_id, domain, service, service_data, description))
            continue

        reply = await _execute_control_action(entity_id, domain, service, service_data, description)
        if duration_minutes and service == "turn_on" and reply.startswith(entity_def["name"]):
            asyncio.create_task(
                _auto_turn_off_later(sender, entity_id, domain, entity_def["name"], duration_minutes)
            )
            reply += f" (will turn off in {duration_minutes:g} min)"
        immediate_replies.append(reply)

    if pending_actions:
        store_pending(sender, pending_actions)
        descriptions = ", ".join(a.description for a in pending_actions)
        logger.info("Risky actions pending confirmation: %s", descriptions)
        immediate_replies.append(f"This will: {descriptions}. Reply 'yes' to confirm.")

    if immediate_replies:
        await send_message(sender, "\n".join(immediate_replies))
