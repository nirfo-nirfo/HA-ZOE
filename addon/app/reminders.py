import calendar
import json
import time
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app.logging_config import logger
from app.settings import settings

_IL_TZ = ZoneInfo("Asia/Jerusalem")

# Allowed recurrence values. A reminder with recurrence=None fires once; any of
# these reschedules itself to the next occurrence after firing.
RECURRENCES = {"daily", "weekly", "monthly", "yearly"}


@dataclass
class Reminder:
    id: str
    sender: str
    text: str
    send_at: float  # Unix timestamp
    recurrence: str | None = None  # None | daily | weekly | monthly | yearly


def _add_period(dt: datetime, recurrence: str) -> datetime:
    """Advances a naive local datetime by one recurrence period, keeping the
    wall-clock time stable across DST and clamping to the end of short months."""
    if recurrence == "daily":
        return dt + timedelta(days=1)
    if recurrence == "weekly":
        return dt + timedelta(weeks=1)
    if recurrence == "monthly":
        month = dt.month % 12 + 1
        year = dt.year + (1 if dt.month == 12 else 0)
        day = min(dt.day, calendar.monthrange(year, month)[1])
        return dt.replace(year=year, month=month, day=day)
    if recurrence == "yearly":
        year = dt.year + 1
        day = min(dt.day, calendar.monthrange(year, dt.month)[1])
        return dt.replace(year=year, day=day)
    return dt


def _next_occurrence(send_at: float, recurrence: str, now: float) -> float:
    """Returns the first occurrence strictly after `now`, so a reminder missed
    while the add-on was down catches up to the future instead of firing repeatedly."""
    dt = datetime.fromtimestamp(send_at, _IL_TZ).replace(tzinfo=None)
    while True:
        dt = _add_period(dt, recurrence)
        ts = dt.replace(tzinfo=_IL_TZ).timestamp()
        if ts > now:
            return ts


def next_annual_occurrence(send_at: float, now: float | None = None) -> float:
    """Returns the earliest yearly occurrence (same month/day/time as `send_at`)
    that is strictly after `now`. Used to fix a wrong year on a yearly reminder:
    the model picks the year and can slip (e.g. tomorrow's birthday stored as next
    year), so the code derives the correct next occurrence from month/day/time."""
    now = time.time() if now is None else now
    target = datetime.fromtimestamp(send_at, _IL_TZ).replace(tzinfo=None)
    now_year = datetime.fromtimestamp(now, _IL_TZ).year
    ts = send_at
    for year in (now_year, now_year + 1, now_year + 2):
        day = min(target.day, calendar.monthrange(year, target.month)[1])
        ts = target.replace(year=year, day=day, tzinfo=_IL_TZ).timestamp()
        if ts > now:
            return ts
    return ts


def normalize_recurring() -> int:
    """One-time self-heal for yearly reminders stored further out than their true
    next occurrence (the wrong-year-at-creation bug). Only ever pulls a reminder
    EARLIER, never delays one. Returns how many were corrected."""
    now = time.time()
    reminders = _load()
    changed = 0
    for i, r in enumerate(reminders):
        if r.recurrence == "yearly":
            corrected = next_annual_occurrence(r.send_at, now)
            if corrected < r.send_at:
                reminders[i] = replace(r, send_at=corrected)
                changed += 1
    if changed:
        _save(reminders)
    return changed


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


def find_duplicate(
    sender: str, text: str, send_at: float, recurrence: str | None = None
) -> Reminder | None:
    """Returns an already-scheduled reminder identical to the one being added.

    Meta sometimes redelivers the same user request as a fresh message with a new
    id, which the webhook-level dedup in whatsapp.py cannot catch. Matching on the
    reminder itself keeps that from producing duplicate alerts.
    """
    for r in _load():
        if (
            r.sender == sender
            and r.text == text
            and r.send_at == send_at
            and r.recurrence == recurrence
        ):
            return r
    return None


def add_reminder(
    sender: str, text: str, send_at: float, recurrence: str | None = None
) -> Reminder:
    reminders = _load()
    reminder = Reminder(
        id=str(uuid.uuid4())[:8],
        sender=sender,
        text=text,
        send_at=send_at,
        recurrence=recurrence,
    )
    reminders.append(reminder)
    _save(reminders)
    return reminder


def list_reminders(sender: str, kind: str = "all") -> list[Reminder]:
    """Returns the user's pending reminders, sorted by time.

    kind filters by recurrence: 'all' (default), 'one_time' for non-recurring
    reminders only, or one of RECURRENCES for a specific repeat interval.
    """
    now = time.time()
    items = [r for r in _load() if r.sender == sender and r.send_at > now]
    if kind == "one_time":
        items = [r for r in items if not r.recurrence]
    elif kind in RECURRENCES:
        items = [r for r in items if r.recurrence == kind]
    return sorted(items, key=lambda r: r.send_at)


def find_matching(sender: str, identifier: str) -> list[Reminder]:
    """Resolves a reminder from a snippet of its text OR its exact id.

    Returns the pending (future) reminders that match, so callers can act by
    description ('the soccer signup reminder') without knowing the id. An exact
    id match wins outright; otherwise it's a case-insensitive substring on text.
    """
    ident = identifier.strip()
    now = time.time()
    pending = [r for r in _load() if r.sender == sender and r.send_at > now]
    by_id = [r for r in pending if r.id == ident]
    if by_id:
        return by_id
    needle = ident.lower()
    return [r for r in pending if needle and needle in r.text.lower()]


def reschedule(reminder_id: str, sender: str, send_at: float) -> bool:
    """Moves an existing reminder to a new time, keeping its text and recurrence."""
    reminders = _load()
    for i, r in enumerate(reminders):
        if r.id == reminder_id and r.sender == sender:
            reminders[i] = replace(r, send_at=send_at)
            _save(reminders)
            return True
    return False


def delete_reminder(reminder_id: str, sender: str) -> bool:
    reminders = _load()
    new = [r for r in reminders if not (r.id == reminder_id and r.sender == sender)]
    if len(new) == len(reminders):
        return False
    _save(new)
    return True


def delete_all_reminders(sender: str) -> int:
    reminders = _load()
    remaining = [r for r in reminders if r.sender != sender]
    deleted = len(reminders) - len(remaining)
    if deleted:
        _save(remaining)
    return deleted


def pop_due() -> list[Reminder]:
    """Returns reminders whose time has come. One-shot reminders are removed;
    recurring ones are rescheduled to their next occurrence and kept."""
    now = time.time()
    reminders = _load()
    due = [r for r in reminders if r.send_at <= now]
    if not due:
        return []
    remaining = [r for r in reminders if r.send_at > now]
    for r in due:
        if r.recurrence in RECURRENCES:
            remaining.append(replace(r, send_at=_next_occurrence(r.send_at, r.recurrence, now)))
    _save(remaining)
    return due
