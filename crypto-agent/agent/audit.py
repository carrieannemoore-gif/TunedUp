"""Append-only JSONL audit log: every decision, verdict, order and error, for taxes and disputes."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SECRET_KEYS = {"rh_private_key_b64", "rh_api_key", "x-api-key", "x-signature", "api_key", "private_key"}


def _redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: ("<redacted>" if k.lower() in _SECRET_KEYS else _redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


class AuditLog:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, event: str, **data: Any) -> None:
        entry = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **_redact(data)}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
