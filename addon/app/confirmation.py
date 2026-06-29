import time
from dataclasses import dataclass

from app.settings import settings


@dataclass
class PendingAction:
    entity_id: str
    domain: str
    service: str
    service_data: dict
    description: str
    expires_at: float


CONFIRM_WORDS = {"yes", "confirm", "כן", "אישור"}

_pending: dict[str, list[PendingAction]] = {}


def store_pending(sender: str, actions: list[PendingAction]) -> None:
    _pending[sender] = actions


def pop_if_confirmed(sender: str, message_text: str) -> list[PendingAction] | None:
    """Returns the pending actions if message_text confirms them and they haven't expired."""
    actions = _pending.get(sender)
    if not actions:
        return None

    if time.time() > actions[0].expires_at:
        del _pending[sender]
        return None

    if message_text.strip().lower() not in CONFIRM_WORDS:
        return None

    del _pending[sender]
    return actions


def make_pending(
    entity_id: str, domain: str, service: str, service_data: dict, description: str
) -> PendingAction:
    return PendingAction(
        entity_id=entity_id,
        domain=domain,
        service=service,
        service_data=service_data,
        description=description,
        expires_at=time.time() + settings.confirmation_ttl_seconds,
    )
