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

_pending: dict[str, PendingAction] = {}


def store_pending(sender: str, action: PendingAction) -> None:
    _pending[sender] = action


def pop_if_confirmed(sender: str, message_text: str) -> PendingAction | None:
    """Returns the pending action if message_text confirms it and it hasn't expired."""
    action = _pending.get(sender)
    if action is None:
        return None

    if time.time() > action.expires_at:
        del _pending[sender]
        return None

    if message_text.strip().lower() not in CONFIRM_WORDS:
        return None

    del _pending[sender]
    return action


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
