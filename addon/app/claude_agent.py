from pathlib import Path
from typing import Any

import yaml
from anthropic import Anthropic

from app.settings import settings

_client = Anthropic(api_key=settings.anthropic_api_key)

_CONTROL_TOOL = "control_device"
_STATUS_TOOL = "get_device_status"

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
    "For a cover entity, to set a specific open percentage use service=set_cover_position "
    "with service_data={\"position\": <0-100>}, where 0 is fully closed and 100 is fully open. "
    "For anything that is not about a known device — general questions, writing or "
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

    message = _client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        tools=tools,
        messages=[
            {
                "role": "user",
                "content": f"Known devices:\n{entity_summary}\n\nUser message: {user_text}",
            }
        ],
    )

    tool_calls = [
        {"tool": block.name, "input": block.input}
        for block in message.content
        if block.type == "tool_use" and block.name in (_CONTROL_TOOL, _STATUS_TOOL)
    ]
    text = "".join(block.text for block in message.content if block.type == "text").strip()
    return tool_calls, text
