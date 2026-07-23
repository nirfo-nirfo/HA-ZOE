from datetime import datetime
from zoneinfo import ZoneInfo

_IL_TZ = ZoneInfo("Asia/Jerusalem")
from pathlib import Path
from typing import Any

import yaml
from anthropic import Anthropic

from app.settings import settings

_client = Anthropic(api_key=settings.anthropic_api_key)

_CONTROL_TOOL = "control_device"
_STATUS_TOOL = "get_device_status"
_SET_REMINDER = "set_reminder"
_LIST_REMINDERS = "list_reminders"
_DELETE_REMINDER = "delete_reminder"
_DELETE_ALL_REMINDERS = "delete_all_reminders"

_ADD_TO_LIST = "add_to_list"
_REMOVE_FROM_LIST = "remove_from_list"
_CLEAR_LIST = "clear_list"
_SHOW_LIST = "show_list"
_SHOW_ALL_LISTS = "show_all_lists"

REMINDER_TOOLS = {_SET_REMINDER, _LIST_REMINDERS, _DELETE_REMINDER, _DELETE_ALL_REMINDERS}
LIST_TOOLS = {_ADD_TO_LIST, _REMOVE_FROM_LIST, _CLEAR_LIST, _SHOW_LIST, _SHOW_ALL_LISTS}

SYSTEM_PROMPT = (
    "You are ZOE, a personal assistant reachable over WhatsApp that also controls "
    "Home Assistant. You are given a list of known smart-home devices (entities) with "
    "their current state. "
    "When the user asks you to do something to one of those devices, call the "
    "control_device tool with the exact entity_id, domain, and service from the device "
    "list. "
    "When the user asks about the current state of a device instead of asking you to "
    "change it, call the get_device_status tool with that entity_id instead. "
    "If the user's message implies acting on more than one device (e.g. \"close both "
    "shutters\"), call control_device once per device, in the same turn. "
    "Only act on devices in the list — never invent an entity_id. "
    "If an entity with domain=automation matches what the user is asking for (by name or "
    "clear intent, e.g. a routine like \"morning\" or \"leaving the house\"), call "
    "control_device on that automation with service=trigger, and do NOT also separately "
    "control other individual devices yourself — the automation already does whatever it "
    "is configured to do. Only control individual devices directly when no matching "
    "automation exists for what the user asked. "
    "For a cover entity, to set a specific open percentage use service=set_cover_position "
    "with service_data={\"position\": <0-100>}, where 0 is fully closed and 100 is fully open. "
    "If the user asks to turn something on for a specific duration (e.g. \"turn on the "
    "boiler for an hour\"), call control_device with service=turn_on and also set "
    "duration_minutes to that many minutes — ZOE will turn it back off automatically when "
    "the time is up. Only set duration_minutes together with turn_on. "
    "When the user asks to be reminded about something, call set_reminder with the reminder "
    "text and the exact ISO 8601 datetime (e.g. 2026-07-03T09:00:00). Use the current "
    "datetime provided in the context to resolve relative times like 'tomorrow', 'in 2 hours', "
    "'next Sunday'. All times are in Israel time (Asia/Jerusalem). "
    "When the user gives a calendar date without a year (e.g. '8th of January'), always pick "
    "the next occurrence of that date in the future — if it has already passed this year, use "
    "next year. send_at must never be in the past. "
    "When the user asks to see their reminders, call list_reminders. "
    "When the user asks to delete or cancel a specific reminder, call delete_reminder with the reminder id. "
    "When the user asks to delete or cancel ALL reminders, call delete_all_reminders. "
    "For shared lists: use add_to_list to add an item, remove_from_list to remove an item by "
    "its text, clear_list to wipe the whole list, show_list to display one list, and "
    "show_all_lists to see the names of all existing lists. "
    "The user can have any number of lists on any topic — use whatever list name the user "
    "names (e.g. 'ספרים', 'סרטים לראות', 'מתנות', 'packing'). Derive list_name from the user's "
    "own wording and keep it consistent for that same list across messages. "
    "Two names are canonical so the common lists don't fragment: use list_name='shopping' for "
    "grocery/shopping lists ('רשימת קניות', 'shopping list'), and list_name='tasks' for "
    "to-do/task lists ('משימות', 'tasks', 'to-do'). For every other topic, use the user's name for it. "
    "If the user refers to a list whose exact name you are unsure of, call show_all_lists first "
    "to see what exists rather than guessing or creating a near-duplicate. "
    "Lists are shared between all family members. "
    "For anything that is not about a known device, reminder, or list — general questions, writing or "
    "drafting text, current events, weather, or any other normal personal-assistant "
    "request — do not call any tool. Just answer directly and naturally in plain text, "
    "the same way you would in a normal conversation. Use the web_search tool when you "
    "need current or real-world information you would otherwise be unsure about. Reply "
    "in whatever language the user wrote in."
)


def _load_entities() -> list[dict[str, Any]]:
    path = Path(settings.entities_config_path)
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data["entities"]


def get_known_entities() -> dict[str, dict[str, Any]]:
    return {e["entity_id"]: e for e in _load_entities()}


def _build_tools(entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entity_ids = [e["entity_id"] for e in entities]
    services = sorted({s for e in entities for s in e["services"]})
    return [
        {
            "name": _CONTROL_TOOL,
            "description": "Calls a Home Assistant service on a known entity.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string", "enum": entity_ids},
                    "domain": {"type": "string"},
                    "service": {"type": "string", "enum": services},
                    "service_data": {
                        "type": "object",
                        "description": "Optional extra service parameters (e.g. position).",
                    },
                    "duration_minutes": {
                        "type": "number",
                        "description": "If set with service=turn_on, automatically turn the "
                        "device back off after this many minutes.",
                    },
                },
                "required": ["entity_id", "domain", "service"],
            },
        },
        {
            "name": _STATUS_TOOL,
            "description": "Reports the current state of a known entity, without changing it.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string", "enum": entity_ids},
                },
                "required": ["entity_id"],
            },
        },
        {
            "name": _SET_REMINDER,
            "description": "Saves a reminder that ZOE will send to the user at the specified time.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The reminder message to send."},
                    "send_at": {
                        "type": "string",
                        "description": "ISO 8601 datetime when to send the reminder, e.g. 2026-07-03T09:00:00.",
                    },
                },
                "required": ["text", "send_at"],
            },
        },
        {
            "name": _LIST_REMINDERS,
            "description": "Returns all pending reminders for the user.",
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": _DELETE_REMINDER,
            "description": "Deletes a pending reminder by its id.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "The reminder id to delete."},
                },
                "required": ["id"],
            },
        },
        {
            "name": _DELETE_ALL_REMINDERS,
            "description": "Deletes ALL pending reminders for the user at once.",
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": _ADD_TO_LIST,
            "description": "Adds an item to a named shared list (e.g. 'shopping', 'tasks').",
            "input_schema": {
                "type": "object",
                "properties": {
                    "list_name": {"type": "string", "description": "Name of the list, e.g. 'shopping' or 'tasks'."},
                    "text": {"type": "string", "description": "The item text to add."},
                },
                "required": ["list_name", "text"],
            },
        },
        {
            "name": _REMOVE_FROM_LIST,
            "description": "Removes items matching the given text from a named list (case-insensitive substring match).",
            "input_schema": {
                "type": "object",
                "properties": {
                    "list_name": {"type": "string"},
                    "text": {"type": "string", "description": "Text to match against items. All matching items are removed."},
                },
                "required": ["list_name", "text"],
            },
        },
        {
            "name": _CLEAR_LIST,
            "description": "Removes all items from a named list.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "list_name": {"type": "string"},
                },
                "required": ["list_name"],
            },
        },
        {
            "name": _SHOW_LIST,
            "description": "Shows all current items in a named list.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "list_name": {"type": "string"},
                },
                "required": ["list_name"],
            },
        },
        {
            "name": _SHOW_ALL_LISTS,
            "description": "Lists the names of all existing lists and how many items each has. "
            "Use when the user asks what lists they have (e.g. 'what lists do I have?', 'איזה רשימות יש לי?').",
            "input_schema": {"type": "object", "properties": {}},
        },
        {"type": "web_search_20250305", "name": "web_search", "max_uses": 3},
    ]


def decide_actions(user_text: str, states: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    """Returns (tool_calls, assistant_text). tool_calls is a list of
    {"tool": <name>, "input": <dict>}; assistant_text is any plain-text reply Claude
    produced (used as a general-assistant answer when no device tool was called)."""
    entities = _load_entities()
    tools = _build_tools(entities)

    entity_summary = "\n".join(
        f"- {e['entity_id']} ({e['name']}): domain={e['domain']}, "
        f"services={e['services']}, state={states.get(e['entity_id'], {}).get('state', 'unknown')}"
        for e in entities
    )

    now_str = datetime.now(_IL_TZ).strftime("%Y-%m-%dT%H:%M:%S")

    message = _client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        tools=tools,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Current datetime (Israel): {now_str}\n"
                    f"Known devices:\n{entity_summary}\n\n"
                    f"User message: {user_text}"
                ),
            }
        ],
    )

    tool_calls = [
        {"tool": block.name, "input": block.input}
        for block in message.content
        if block.type == "tool_use"
        and block.name in (_CONTROL_TOOL, _STATUS_TOOL, *REMINDER_TOOLS, *LIST_TOOLS)
    ]
    text = "".join(block.text for block in message.content if block.type == "text").strip()
    return tool_calls, text
