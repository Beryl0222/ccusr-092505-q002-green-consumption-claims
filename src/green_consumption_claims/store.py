"""事件存储：JSONL 追加写与重放，支撑平台重启后恢复受理与期限。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Protocol


class EventStore(Protocol):
    """领域事件的追加式存储。"""

    def append(self, event: Mapping[str, Any]) -> None: ...

    def load(self) -> list[dict[str, Any]]: ...


class InMemoryEventStore:
    """进程内事件存储，用于测试与联调。"""

    def __init__(self) -> None:
        self._events: list[dict[str, Any]] = []

    def append(self, event: Mapping[str, Any]) -> None:
        self._events.append(dict(event))

    def load(self) -> list[dict[str, Any]]:
        return [dict(event) for event in self._events]


class JsonlEventStore:
    """以 JSONL 文件持久化事件，重启后按序重放。"""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def append(self, event: Mapping[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(event), ensure_ascii=False, sort_keys=True) + "\n")

    def load(self) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        return [
            json.loads(line)
            for line in self._path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
