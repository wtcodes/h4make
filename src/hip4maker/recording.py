"""Structured JSONL event recording."""

from __future__ import annotations

import json
import sys
import threading
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, TextIO


class JsonlRecorder:
    def __init__(self, path: str | Path | None = None) -> None:
        self._owns_stream = path is not None
        self._stream: TextIO = (
            Path(path).open("a", encoding="utf-8") if path is not None else sys.stdout
        )
        self._lock = threading.Lock()
        self._sequence = 0

    def record(self, event: str, payload: dict[str, Any]) -> None:
        with self._lock:
            self._sequence += 1
            envelope = {
                "ts_ms": time.time_ns() // 1_000_000,
                "sequence": self._sequence,
                "event": event,
                **_json_safe(payload),
            }
            self._stream.write(json.dumps(envelope, sort_keys=True, separators=(",", ":")) + "\n")
            self._stream.flush()

    def record_snapshot(self, payload: dict[str, Any]) -> None:
        """Write timestamped state without event-stream envelope fields."""

        with self._lock:
            envelope = {
                "ts_ms": time.time_ns() // 1_000_000,
                **_json_safe(payload),
            }
            self._stream.write(
                json.dumps(envelope, sort_keys=True, separators=(",", ":")) + "\n"
            )
            self._stream.flush()

    def close(self) -> None:
        if self._owns_stream:
            self._stream.close()

    def __enter__(self) -> "JsonlRecorder":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    return value
