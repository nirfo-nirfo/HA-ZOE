from pathlib import Path
from typing import Any

import yaml
from anthropic import Anthropic

from app.settings import settings

_client = Anthropic(api_key=settings.anthropic_api_key)

_CONTROL_TOOL = "control_device"
_STATUS_TOOL = "get_device_status"

SYSTEM_PROMPT = (
    "You are ZOE, a Home Assistant control assistant reachable over WhatsApp. "
    "You are given a list of known devices (entities) with their current state. "
    "When the user asks you to do something, call the control_device tool with the "
    "exact entity_id, domain, and service from the device list. "
    "When the user asks about the current state of a device instead of asking you to "
    "change it, call the get_device_status tool with that entity_id instead. "
    "If the user's message implies acting on more than one device (e.g. \"close both "
    "shutters\"), call control_device once per device, in the same turn. "
    "Only act on devices in the list — never invent an entity_id. "
    "If the request is ambiguous or doesn't match a known device, do not call any tool; "
    "instead reply with plain text asking for clarification. "
    "For a cover entity, to set a specific open percentage use service=set_cover_position "
    "with service_data={\"position\": <0-100>}, where 0 is fully closed and 100 is fully open."
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
    ]


def decide_actions(user_text: str, states: dict[str, Any]) -> list[dict[str, Any]]:
    """Returns a list of {"tool": <name>, "input": <dict>} for each tool call Claude made."""
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

    return [
        {"tool": block.name, "input": block.input}
        for block in message.content
        if block.type == "tool_use"
    ]
