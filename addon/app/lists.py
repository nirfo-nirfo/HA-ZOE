import json
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from app.logging_config import logger
from app.settings import settings


@dataclass
class ListItem:
    id: str
    text: str
    added_by: str
    added_at: float


def _load() -> dict[str, list[ListItem]]:
    path = Path(settings.lists_path)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {name: [ListItem(**i) for i in items] for name, items in raw.items()}
    except Exception:
        logger.warning("Could not load lists file, starting fresh")
        return {}


def _save(data: dict[str, list[ListItem]]) -> None:
    path = Path(settings.lists_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({name: [asdict(i) for i in items] for name, items in data.items()}, ensure_ascii=False),
        encoding="utf-8",
    )


def add_item(list_name: str, text: str, sender: str) -> ListItem:
    data = _load()
    item = ListItem(id=str(uuid.uuid4())[:6], text=text, added_by=sender, added_at=time.time())
    data.setdefault(list_name, []).append(item)
    _save(data)
    return item


def remove_items(list_name: str, text: str) -> list[str]:
    """Removes all items whose text contains `text` (case-insensitive). Returns removed texts."""
    data = _load()
    items = data.get(list_name, [])
    needle = text.strip().lower()
    kept, removed = [], []
    for item in items:
        if needle in item.text.lower():
            removed.append(item.text)
        else:
            kept.append(item)
    if removed:
        data[list_name] = kept
        _save(data)
    return removed


def clear_list(list_name: str) -> int:
    data = _load()
    count = len(data.get(list_name, []))
    data[list_name] = []
    _save(data)
    return count


def get_list(list_name: str) -> list[ListItem]:
    return _load().get(list_name, [])
