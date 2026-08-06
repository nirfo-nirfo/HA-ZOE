import asyncio
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_IL_TZ = ZoneInfo("Asia/Jerusalem")

from fastapi import FastAPI, Request, Response

from app.claude_agent import (
    LIST_TOOLS,
    MEMORY_TOOLS,
    REMINDER_TOOLS,
    get_known_entities,
    initial_context,
    run_model,
)
from app.confirmation import make_pending, pop_if_confirmed, store_pending
from app.ha_client import ha_client
from app.logging_config import logger
from app.lists import add_item, clear_list, get_all_list_names, get_list, remove_items
from app.memory import forget, remember
from app.reminders import (
    RECURRENCES,
    add_reminder,
    delete_all_reminders,
    delete_reminder,
    find_duplicate,
    find_matching,
    list_reminders,
    pop_due,
    reschedule,
)
from app.settings import settings
from app.transcribe import transcribe_audio
from app.whatsapp import extract_message, send_message, verify_signature

app = FastAPI(title="ZOE")

# Safety cap on the agentic tool-use loop, so a confused turn can't call tools forever.
_MAX_AGENT_ITERS = 6


@app.on_event("startup")
async def startup() -> None:
    asyncio.create_task(_reminder_loop())


async def _reminder_loop() -> None:
    while True:
        await asyncio.sleep(60)
        # Guard the whole tick: a bad reminder or a failed send must never kill the
        # loop, or reminders would silently stop firing forever while the app stays up.
        try:
            due = pop_due()
        except Exception:
            logger.exception("Reminder loop: pop_due failed")
            continue
        for reminder in due:
            try:
                logger.info("Firing reminder %s for %s", reminder.id, reminder.sender)
                await send_message(reminder.sender, f"⏰ {reminder.text}")
            except Exception:
                logger.exception("Reminder loop: failed to send reminder %s", reminder.id)


@app.get("/debug/reminders")
async def debug_reminders(request: Request) -> Response:
    """Raw reminders.json with human-readable local times, for diagnosing scheduling
    bugs that a paraphrased chat listing can hide. Token-guarded: the Cloudflare
    tunnel makes every path on this port publicly reachable."""
    if request.query_params.get("token") != settings.whatsapp_verify_token:
        return Response(status_code=403)

    path = Path(settings.reminders_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    except Exception as exc:
        return Response(content=f"could not read {path}: {exc}", status_code=500)

    for r in data:
        try:
            r["send_at_israel"] = datetime.fromtimestamp(r["send_at"], tz=_IL_TZ).strftime(
                "%d/%m/%Y %H:%M:%S"
            )
        except Exception:
            r["send_at_israel"] = "unparseable"

    body = {
        "now_israel": datetime.now(_IL_TZ).strftime("%d/%m/%Y %H:%M:%S"),
        "count": len(data),
        "reminders": data,
    }
    return Response(
        content=json.dumps(body, ensure_ascii=False, indent=2),
        media_type="application/json; charset=utf-8",
    )


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
    parsed = extract_message(payload)
    if parsed is None:
        return Response(status_code=200)

    sender, text, audio_id = parsed
    allowed = {n.strip() for n in settings.allowed_sender_numbers.split(",")}
    if sender not in allowed:
        logger.warning("Rejected message from unauthorized sender %s", sender)
        return Response(status_code=200)

    asyncio.create_task(_process_message(sender, text, audio_id))
    return Response(status_code=200)


async def _process_message(sender: str, text: str | None, audio_id: str | None) -> None:
    if audio_id:
        logger.info("Inbound voice from %s, transcribing...", sender)
        text = await transcribe_audio(audio_id)
        if not text:
            await send_message(sender, "Sorry, I couldn't understand the voice message.")
            return
        logger.info("Transcribed voice from %s: %s", sender, text)

    logger.info("Inbound from %s: %s", sender, text)
    await _handle_message(sender, text)


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


def _fmt_reminder(r) -> str:
    when = datetime.fromtimestamp(r.send_at, tz=_IL_TZ).strftime("%d/%m/%Y %H:%M")
    return f"• [{r.id}] {when} — {r.text}"


# (key in reminder.recurrence, section title) in display order; one_time = no recurrence.
_REMINDER_GROUPS = [
    ("one_time", "One-time"),
    ("daily", "🔁 Daily"),
    ("weekly", "🔁 Weekly"),
    ("monthly", "🔁 Monthly"),
    ("yearly", "🔁 Yearly"),
]


def _handle_reminder_call(sender: str, tool: str, inp: dict) -> str:
    if tool == "set_reminder":
        try:
            dt = datetime.fromisoformat(inp["send_at"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_IL_TZ)
            send_at = dt.timestamp()
        except (ValueError, KeyError):
            return "I couldn't parse that date/time — please try again."
        if send_at <= datetime.now(tz=_IL_TZ).timestamp():
            # Would fire on the next scheduler tick instead of at the intended time —
            # usually means the year was guessed wrong on a date given without one.
            when = dt.astimezone(_IL_TZ).strftime("%d/%m/%Y %H:%M")
            return (
                f"That time ({when}) is in the past, so I didn't set the reminder. "
                "Please tell me the date again, including the year."
            )
        recurrence = inp.get("recurrence")
        if recurrence not in RECURRENCES:
            recurrence = None
        existing = find_duplicate(sender, inp["text"], send_at, recurrence)
        if existing:
            when = datetime.fromtimestamp(existing.send_at, tz=_IL_TZ).strftime("%d/%m/%Y %H:%M")
            return f"You already have that reminder set for {when} — keeping the existing one."
        reminder = add_reminder(sender, inp["text"], send_at, recurrence)
        when = datetime.fromtimestamp(reminder.send_at, tz=_IL_TZ).strftime("%d/%m/%Y %H:%M")
        repeat = f" (repeats {recurrence})" if recurrence else ""
        return f"Reminder set ✅{repeat} — I'll message you on {when}: {reminder.text}"

    if tool == "list_reminders":
        kind = inp.get("kind", "all")
        if kind != "one_time" and kind not in RECURRENCES:
            kind = "all"
        pending = list_reminders(sender, kind)
        if not pending:
            if kind == "all":
                return "You have no pending reminders."
            label = "one-time" if kind == "one_time" else kind
            return f"You have no {label} reminders."
        if kind != "all":
            label = "one-time" if kind == "one_time" else kind
            return f"Your {label} reminders:\n" + "\n".join(_fmt_reminder(r) for r in pending)
        # No filter: group by type so recurring reminders don't bury the one-off ones.
        sections = []
        for key, title in _REMINDER_GROUPS:
            bucket = [r for r in pending if (r.recurrence or "one_time") == key]
            if bucket:
                sections.append(f"{title}:\n" + "\n".join(_fmt_reminder(r) for r in bucket))
        return "Your reminders:\n\n" + "\n\n".join(sections)

    if tool == "delete_reminder":
        rid = inp.get("id", "")
        if delete_reminder(rid, sender):
            return f"Reminder {rid} deleted ✅"
        return f"Reminder {rid} not found."

    if tool == "delete_reminder_by_text":
        query = inp.get("text", "").strip()
        if not query:
            return "Which reminder should I cancel?"
        matches = find_matching(sender, query)
        if not matches:
            return f"I couldn't find a reminder matching '{query}'."
        if len(matches) > 1:
            return (
                f"Several reminders match '{query}' — which one? Reply with its id:\n"
                + "\n".join(_fmt_reminder(r) for r in matches)
            )
        r = matches[0]
        delete_reminder(r.id, sender)
        when = datetime.fromtimestamp(r.send_at, tz=_IL_TZ).strftime("%d/%m/%Y %H:%M")
        return f"Cancelled ✅ — {when}: {r.text}"

    if tool == "reschedule_reminder":
        query = inp.get("text", "").strip()
        try:
            dt = datetime.fromisoformat(inp["send_at"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_IL_TZ)
            send_at = dt.timestamp()
        except (ValueError, KeyError):
            return "I couldn't parse that new date/time — please try again."
        if send_at <= datetime.now(tz=_IL_TZ).timestamp():
            when = dt.astimezone(_IL_TZ).strftime("%d/%m/%Y %H:%M")
            return f"That new time ({when}) is in the past, so I didn't move the reminder."
        if not query:
            return "Which reminder should I move?"
        matches = find_matching(sender, query)
        if not matches:
            return f"I couldn't find a reminder matching '{query}'."
        if len(matches) > 1:
            return (
                f"Several reminders match '{query}' — which one? Reply with its id:\n"
                + "\n".join(_fmt_reminder(r) for r in matches)
            )
        r = matches[0]
        reschedule(r.id, sender, send_at)
        when = datetime.fromtimestamp(send_at, tz=_IL_TZ).strftime("%d/%m/%Y %H:%M")
        return f"Moved ✅ — now {when}: {r.text}"

    if tool == "delete_all_reminders":
        count = delete_all_reminders(sender)
        return f"All {count} reminder(s) deleted ✅" if count else "No reminders to delete."

    return ""


def _handle_list_call(sender: str, tool: str, inp: dict) -> str:
    list_name = inp.get("list_name", "")

    if tool == "add_to_list":
        text = inp.get("text", "").strip()
        if not text:
            return "What should I add to the list?"
        add_item(list_name, text, sender)
        return f"Added to {list_name}: {text} ✅"

    if tool == "remove_from_list":
        text = inp.get("text", "").strip()
        removed = remove_items(list_name, text)
        if removed:
            return f"Removed from {list_name}: {', '.join(removed)} ✅"
        return f"No items matching '{text}' found in {list_name}."

    if tool == "clear_list":
        count = clear_list(list_name)
        return f"{list_name.capitalize()} list cleared ({count} item(s)) ✅"

    if tool == "show_list":
        items = get_list(list_name)
        if not items:
            return f"The {list_name} list is empty."
        lines = [f"• {item.text}" for item in items]
        return f"{list_name.capitalize()} list:\n" + "\n".join(lines)

    if tool == "show_all_lists":
        names = get_all_list_names()
        if not names:
            return "You don't have any lists yet."
        lines = [f"• {name} ({count})" for name, count in names]
        return "Your lists:\n" + "\n".join(lines)

    return ""


def _handle_memory_call(tool: str, inp: dict) -> str:
    text = inp.get("text", "").strip()
    if tool == "remember":
        if not text:
            return "Nothing to remember."
        fact = remember(text)
        if fact is None:
            return f"Already in memory: {text}"
        return f"Remembered: {text}"

    if tool == "forget":
        removed = forget(text)
        if removed:
            return "Forgot: " + "; ".join(removed)
        return f"No remembered fact matching '{text}'."

    return ""


async def _dispatch_tool(
    sender: str, tool: str, inp: dict, known_entities: dict, pending_actions: list
) -> str:
    """Executes one tool call and returns a result string fed back to the model.
    Risky device actions are not executed here — they're queued for user confirmation."""
    if tool in REMINDER_TOOLS:
        return _handle_reminder_call(sender, tool, inp)

    if tool in LIST_TOOLS:
        return _handle_list_call(sender, tool, inp)

    if tool in MEMORY_TOOLS:
        return _handle_memory_call(tool, inp)

    entity_id = inp.get("entity_id")
    entity_def = known_entities.get(entity_id)
    if entity_def is None:
        logger.error("Model returned unknown entity_id: %s", entity_id)
        return "That device isn't in the known list — refused for safety. Tell the user you can't act on it."

    if tool == "get_device_status":
        live = await ha_client.get_states([entity_id])
        state = live.get(entity_id, {}).get("state", "unknown")
        return f"{entity_def['name']} is currently: {state}"

    if tool == "control_device":
        domain = inp.get("domain")
        service = inp.get("service")
        if not domain or not service:
            return "control_device needs both a domain and a service."
        service_data = inp.get("service_data") or {}
        duration_minutes = inp.get("duration_minutes")
        description = f"{entity_def['name']}: {service}"

        if entity_def.get("risky"):
            pending_actions.append(make_pending(entity_id, domain, service, service_data, description))
            logger.info("Risky action queued for confirmation: %s", description)
            return (
                "This is a sensitive action and must NOT be treated as done. It is queued and will "
                "run only after the user explicitly confirms. Tell the user what will happen and ask "
                "them to reply 'yes' to confirm — do not say it has been done."
            )

        reply = await _execute_control_action(entity_id, domain, service, service_data, description)
        if duration_minutes and service == "turn_on" and "✅" in reply:
            asyncio.create_task(
                _auto_turn_off_later(sender, entity_id, domain, entity_def["name"], duration_minutes)
            )
            reply += f" (will auto turn-off in {duration_minutes:g} min)"
        return reply

    return f"Unknown tool: {tool}"


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

    messages: list = [{"role": "user", "content": initial_context(text, states)}]
    pending_actions: list = []
    final_text = ""

    for _ in range(_MAX_AGENT_ITERS):
        message = await asyncio.to_thread(run_model, messages)
        tool_uses = [b for b in message.content if b.type == "tool_use"]

        if tool_uses:
            messages.append({"role": "assistant", "content": message.content})
            results = []
            for tu in tool_uses:
                logger.info("Tool call: %s %s", tu.name, tu.input)
                result = await _dispatch_tool(sender, tu.name, tu.input, known_entities, pending_actions)
                results.append({"type": "tool_result", "tool_use_id": tu.id, "content": result})
            messages.append({"role": "user", "content": results})
            continue

        if message.stop_reason == "pause_turn":
            # A server tool (web search) paused; re-send to let it resume.
            messages.append({"role": "assistant", "content": message.content})
            continue

        final_text = "".join(b.text for b in message.content if b.type == "text").strip()
        break
    else:
        final_text = final_text or "סליחה, זה נהיה מסובך מדי ולא הצלחתי לסיים."

    if pending_actions:
        store_pending(sender, pending_actions)

    if final_text:
        await send_message(sender, final_text)
    elif not pending_actions:
        await send_message(sender, "I'm not sure what you mean — could you rephrase?")
