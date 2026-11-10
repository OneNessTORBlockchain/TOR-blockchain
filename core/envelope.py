"""The ``{ok, data, error}`` response envelope (Section 7.1).

Every route builds its payload through :func:`ok` so the shape is identical
for the dashboard, the public portal, the docs portal and the Ollama brain.
"""
from __future__ import annotations

from typing import Any

from core.errors import AppError


def ok(data: Any = None, **meta: Any) -> dict:
    """Success envelope. Extra keyword args ride in ``meta``."""
    body: dict[str, Any] = {"ok": True, "data": data, "error": None}
    if meta:
        body["meta"] = meta
    return body


def fail(error: AppError | Exception, **meta: Any) -> dict:
    """Failure envelope — unknown exceptions become INTERNAL, never a leak."""
    if isinstance(error, AppError):
        body: dict[str, Any] = {"ok": False, "data": None,
                                "error": error.to_error()}
    else:  # pragma: no cover - defensive
        body = {"ok": False, "data": None,
                "error": {"code": "INTERNAL", "http_status": 500,
                          "message": "internal error"}}
    if meta:
        body["meta"] = meta
    return body
