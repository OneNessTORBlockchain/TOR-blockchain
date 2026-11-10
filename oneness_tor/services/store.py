"""Tiny JSON persistence for the API layer (sites, invoices, contracts…).

The Rust node owns the chain; these files only hold the *service* records the
node does not model (invoices, generated sites, subscriptions, Tor services).
Every write is atomic (temp file + ``os.replace``) and list collections get
sequential ids like ``inv_0001``.
"""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any

from core.config import settings
from core.errors import NotFound, ValidationError

_LOCK = threading.RLock()
_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,47}$")


def data_dir(*parts: str, create: bool = True) -> Path:
    path = settings.data_dir.joinpath(*parts)
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                         encoding="utf-8")
    os.replace(temporary, path)


def safe_name(value: object, field: str = "name") -> str:
    """A file-safe identifier (wallet names, site ids, model names)."""
    text = str(value or "").strip()
    if not _SAFE.match(text):
        raise ValidationError(
            f"{field} must match {_SAFE.pattern}",
            details={"field": field, "received": text[:64]})
    return text


def slug(value: object, fallback: str = "item") -> str:
    text = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return (text or fallback)[:48]


class Collection:
    """A JSON list with ids, atomic saves and a process-wide lock.

    ``path`` may be a callable so the file follows ``ONENESS_DATA_DIR`` at call
    time instead of being frozen at import time (tests and a second node rely on
    that).
    """

    def __init__(self, path, prefix: str, *, key: str = "id") -> None:
        self._path = path
        self.prefix = prefix
        self.key = key

    @property
    def path(self) -> Path:
        return Path(self._path() if callable(self._path) else self._path)

    def all(self) -> list[dict]:
        with _LOCK:
            rows = read_json(self.path, [])
        return rows if isinstance(rows, list) else []

    def save(self, rows: list[dict]) -> None:
        with _LOCK:
            write_json(self.path, rows)

    def next_id(self, rows: list[dict] | None = None) -> str:
        rows = rows if rows is not None else self.all()
        highest = 0
        pattern = re.compile(rf"^{re.escape(self.prefix)}_(\d+)$")
        for row in rows:
            match = pattern.match(str(row.get(self.key, "")))
            if match:
                highest = max(highest, int(match.group(1)))
        return f"{self.prefix}_{highest + 1:04d}"

    def add(self, row: dict) -> dict:
        with _LOCK:
            rows = self.all()
            row = dict(row)
            row.setdefault(self.key, self.next_id(rows))
            rows.append(row)
            self.save(rows)
            return row

    def get(self, item_id: str) -> dict:
        wanted = str(item_id or "").strip()
        for row in self.all():
            if str(row.get(self.key)) == wanted:
                return row
        raise NotFound(f"{self.prefix} {wanted!r} not found",
                       details={"id": wanted})

    def update(self, item_id: str, **fields: Any) -> dict:
        with _LOCK:
            rows = self.all()
            for index, row in enumerate(rows):
                if str(row.get(self.key)) == str(item_id):
                    updated = {**row, **fields}
                    rows[index] = updated
                    self.save(rows)
                    return updated
        raise NotFound(f"{self.prefix} {item_id!r} not found")

    def delete(self, item_id: str) -> dict:
        """Remove one row (and only that row); returns what was removed."""
        with _LOCK:
            rows = self.all()
            for index, row in enumerate(rows):
                if str(row.get(self.key)) == str(item_id):
                    removed = rows.pop(index)
                    self.save(rows)
                    return removed
        raise NotFound(f"{self.prefix} {item_id!r} not found")

    def find(self, **fields: Any) -> list[dict]:
        return [row for row in self.all()
                if all(row.get(key) == value for key, value in fields.items())]

    def newest(self, limit: int = 25, **fields: Any) -> list[dict]:
        rows = self.find(**fields) if fields else self.all()
        rows = sorted(rows, key=lambda row: str(row.get("created_at", "")),
                      reverse=True)
        return rows[:max(1, int(limit))]
