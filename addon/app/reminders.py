import json
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from app.logging_config import logger
from app.settings import settings


@dataclass
class Reminder:
    id: str
    sender: str
    text: str
    send_at: float  # Unix timestamp


def _load() -> list[Reminder]:
    path = Path(settings.reminders_path)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [Reminder(**r) for r in data]
    except Exception:
        logger.warning("Could not load reminders file, starting fresh")
        return []


def _save(reminders: list[Reminder]) -> None:
    path = Path(settings.reminders_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([asdict(r) for r in reminders], ensure_ascii=False),
        encoding="utf-8",
    )


def add_reminder(sender: str, text: str, send_at: float) -> Reminder:
    reminders = _load()
    reminder = Reminder(id=str(uuid.uuid4())[:8], sender=sender, text=text, send_at=send_at)
    reminders.append(reminder)
    _save(reminders)
    return reminder


def list_reminders(sender: str) -> list[Reminder]:
    return [r for r in _load() if r.sender == sender and r.send_at > time.time()]


def delete_reminder(reminder_id: str, sender: str) -> bool:
    reminders = _load()
    new = [r for r in reminders if not (r.id == reminder_id and r.sender == sender)]
    if len(new) == len(reminders):
        return False
    _save(new)
    return True


def pop_due() -> list[Reminder]:
    now = time.time()
    reminders = _load()
    due = [r for r in reminders if r.send_at <= now]
    if due:
        _save([r for r in reminders if r.send_at > now])
    return due
