"""争议台领域模型、异常与通用工具。

本模块只定义词汇与纯函数，不持有业务状态；业务流程见 service.py。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import Enum
from typing import Any


class DisputeError(Exception):
    """争议台业务错误基类。"""


class NotFoundError(DisputeError):
    """引用的对象不存在。"""


class StateError(DisputeError):
    """当前状态不允许该操作。"""


class AuthorizationError(DisputeError):
    """缺少消费者授权。"""


class TamperError(DisputeError):
    """商家试图改写消费者原始凭证。"""


class EvidenceConflictError(DisputeError):
    """同一凭证出现内容冲突，争议已冻结。"""


class EvidenceClass(str, Enum):
    """凭证分级：事实材料、商家自证、调解结论。"""

    FACT_MATERIAL = "fact_material"
    MERCHANT_SELF_ATTESTATION = "merchant_self_attestation"
    MEDIATION_CONCLUSION = "mediation_conclusion"


class CaseStatus(str, Enum):
    OPEN = "open"
    FROZEN = "frozen"
    DECIDED = "decided"
    REMEDY_CONFIRMED = "remedy_confirmed"


class GreenCategory(str, Enum):
    """绿色行动品类：餐饮、礼盒包装、出行权益、旧物回收。"""

    DINING = "dining"
    GIFT_BOX = "gift_box"
    TRAVEL = "travel"
    RECYCLING = "recycling"


class RemedyKind(str, Enum):
    COMPENSATION = "compensation"
    RECTIFICATION = "rectification"


UPLOADER_CLASSIFICATION = {
    "consumer": EvidenceClass.FACT_MATERIAL,
    "merchant": EvidenceClass.MERCHANT_SELF_ATTESTATION,
    "mediator": EvidenceClass.MEDIATION_CONCLUSION,
}

ORDER_EVIDENCE_KIND = "order_record"

TERMINAL_STATUSES = frozenset({CaseStatus.REMEDY_CONFIRMED.value})


def require_aware(value: datetime, field: str = "时间") -> datetime:
    """拒绝不带时区的时间，与交换契约保持同一口径。"""
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise DisputeError(f"{field}必须是带时区的 datetime")
    return value


def iso(value: datetime) -> str:
    return require_aware(value).isoformat()


def parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise DisputeError(f"无法解析时间: {value!r}") from exc
    return require_aware(parsed)


def content_digest(content: Any) -> str:
    """对凭证内容生成稳定摘要，用于幂等判重与冲突检测。"""
    if isinstance(content, str):
        canonical = content
    else:
        canonical = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def pseudonym(consumer_id: str) -> str:
    """调解员视图中消费者的化名，避免直接暴露账号。"""
    return "C-" + hashlib.sha256(consumer_id.encode("utf-8")).hexdigest()[:10]


def mask_contact(contact: dict[str, Any]) -> dict[str, Any]:
    """联系方式最小化：调解员只需知道存在联系方式，不需要原文。"""
    masked: dict[str, Any] = {}
    for key, value in contact.items():
        if not isinstance(value, str) or not value:
            masked[key] = value
        elif key == "phone" and len(value) >= 7:
            masked[key] = value[:3] + "****" + value[-4:]
        else:
            masked[key] = "***"
    return masked
