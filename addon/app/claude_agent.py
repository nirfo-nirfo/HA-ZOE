from pathlib import Path
from typing import Any

import yaml
from anthropic import Anthropic

from app.settings import settings

_client = Anthropic(api_key=settings.anthropic_api_key)

_TOOL_NAME = "control_device"

SYSTEM_PROMPT = (
    "You are ZOE, a Home Assistant control assistant reachable over WhatsApp. "
    "You are given a list of known devices (entities) with their current state. "
    "When the user asks you to do something, call the control_device tool with the "
    "exact entity_id, domain, and service from the device list. "
    "Only act on devices in the list — never invent an entity_id. "
    "If the request is ambiguous or doesn't match a known device, do not call the tool; "
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


def _build_tool_schema(entities: list[dict[str, Any]]) -> dict[str, Any]:
    entity_ids = [e["entity_id"] for e in entities]
    services = sorted({s for e in entities for s in e["services"]})
    return {
        "name": _TOOL_NAME,
        "description": "Calls a Home Assistant service on a known entity.",
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string", "enum": entity_ids},
                "domain": {"type": "string"},
                "service": {"type": "string", "enum": services},
                "service_data": {
                    "type": "object",
                    "description": "Optional extra service parameters (e.g. brightness).",
                },
            },
            "required": ["entity_id", "domain", "service"],
        },
    }


def decide_action(
    user_text: str, states: dict[str, Any]
) -> dict[str, Any] | None:
    """Returns a tool_use input dict, or None if Claude responded with plain text."""
    entities = _load_entities()
    tool = _build_tool_schema(entities)

    entity_summary = "\n".join(
        f"- {e['entity_id']} ({e['name']}): domain={e['domain']}, "
        f"services={e['services']}, state={states.get(e['entity_id'], {}).get('state', 'unknown')}"
        for e in entities
    )

    message = _client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        tools=[tool],
        messages=[
            {
                "role": "user",
                "content": f"Known devices:\n{entity_summary}\n\nUser message: {user_text}",
            }
        ],
    )

    for block in message.content:
        if block.type == "tool_use" and block.name == _TOOL_NAME:
            return block.input

    return None
