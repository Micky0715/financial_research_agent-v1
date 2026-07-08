"""File naming and JSON/text persistence helpers."""
import json
import re
from pathlib import Path
from typing import Any


def sanitize_filename(name: str, max_len: int = 60) -> str:
    """Turn arbitrary text (including Chinese/special chars) into a safe filename fragment.

    Keeps CJK characters, ASCII letters/digits, dash and underscore; everything
    else becomes an underscore. Truncates to max_len to avoid path length issues.
    """
    if not name:
        return "untitled"
    cleaned = re.sub(r"[^\w一-鿿\-]+", "_", name, flags=re.UNICODE)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return (cleaned or "untitled")[:max_len]


def save_json(path: Path, data: Any) -> None:
    """Write data as UTF-8 pretty-printed JSON, creating parent dirs if needed."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)


def save_text(path: Path, text: str) -> None:
    """Write plain text as UTF-8, creating parent dirs if needed."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def load_json(path: Path) -> Any:
    """Read a JSON file as UTF-8."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
