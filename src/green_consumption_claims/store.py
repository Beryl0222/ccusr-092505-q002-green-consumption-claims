"""争议台状态的本机持久化。

采用整份 JSON 快照 + 临时文件原子替换：平台重启后从同一文件恢复，
已保存的受理结果与期限不丢失。文件即事实来源，不依赖外部服务。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def empty_state() -> dict[str, Any]:
    return {
        "claims": {},      # claim_id -> {版本字符串 -> 承诺快照}
        "rules": [],       # 规则版本，按 version 升序
        "orders": {},      # order_id -> 订单（含各部分与变更历史）
        "cases": {},       # case_id -> 争议案件
        "evidence": {},    # evidence_id -> 凭证（全局幂等键）
        "receipts": {},    # receipt_id -> 受理回执
        "audit": [],       # 处理轨迹：谁、何时、做了什么
        "outbox": [],      # 待外发通知（外部投诉渠道恢复后补发）
        "seq": 0,          # 单调序号，生成回执/案件/日志编号
    }


class JsonStore:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> dict[str, Any]:
        state = empty_state()
        if self._path.exists():
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError(f"状态文件损坏: {self._path}")
            state.update(data)
        return state

    def save(self, state: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        tmp.replace(self._path)
