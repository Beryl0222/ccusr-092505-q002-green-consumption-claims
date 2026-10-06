"""绿色消费凭证争议台的领域模型与纯逻辑。

本模块只包含不可变值对象、枚举和不依赖存储的判定函数；
受理、冻结、调解等状态变更见 platform.py。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Mapping


class MaterialClass(str, Enum):
    """材料类别：事实材料、商家自证、调解结论。"""

    FACTUAL = "事实材料"
    MERCHANT_SELF = "商家自证"
    MEDIATION_CONCLUSION = "调解结论"


class EvidenceKind(str, Enum):
    """凭证种类。"""

    ORDER_RECORD = "订单"
    CHAT_RECORD = "聊天记录"
    PACKAGING_PHOTO = "包装照片"
    RECYCLING_HANDOVER = "回收交接"
    LOW_CARBON_SERVICE = "低碳服务凭证"
    NEGOTIATION_RECORD = "协商过程"


class LineCategory(str, Enum):
    """订单条目类别。"""

    DINING = "餐饮"
    GIFT_BOX = "礼盒"
    TRAVEL = "出行权益"


class ActorRole(str, Enum):
    """提交或处理材料的角色。"""

    CONSUMER = "consumer"
    MERCHANT = "merchant"
    INTAKE = "intake"
    MEDIATOR = "mediator"
    SYSTEM = "system"


class CaseStatus(str, Enum):
    OPEN = "OPEN"
    FROZEN = "FROZEN"
    REMEDY_CONFIRMED = "REMEDY_CONFIRMED"


class OrderChangeType(str, Enum):
    REFUND = "退款"
    REBOOK = "改签"


class RemedyType(str, Enum):
    COMPENSATION = "补偿"
    RECTIFICATION = "整改"


class IntakeOutcome(str, Enum):
    """凭证受理结果。"""

    ACCEPTED = "ACCEPTED"
    DUPLICATE = "DUPLICATE"
    CONFLICT_FROZEN = "CONFLICT_FROZEN"


SCOPE_ORDER_EVIDENCE = "order_evidence"
SCOPE_OPEN_CASE = "open_case"


@dataclass(frozen=True)
class Authorization:
    """消费者对平台使用其材料的授权。"""

    consumer_id: str
    scopes: frozenset[str]
    granted_at: datetime

    def allows(self, scope: str, consumer_id: str) -> bool:
        return self.consumer_id == consumer_id and scope in self.scopes


@dataclass(frozen=True)
class PromisedAction:
    """商家承诺的一项绿色行动。"""

    category: LineCategory
    action: str
    detail: str


@dataclass(frozen=True)
class ClaimVersion:
    """商家承诺的一个版本，含适用商品与适用时间。"""

    version: int
    published_at: datetime
    window_start: datetime
    window_end: datetime
    applicable_products: frozenset[str]
    promised_actions: tuple[PromisedAction, ...]
    summary: str


@dataclass(frozen=True)
class GreenRule:
    """一条绿色行动计入规则。"""

    category: LineCategory
    action: str
    description: str


@dataclass(frozen=True)
class RuleVersion:
    """规则的一个版本，只增不改。"""

    version: int
    issued_at: datetime
    rules: tuple[GreenRule, ...]


@dataclass(frozen=True)
class OrderLine:
    line_id: str
    category: LineCategory
    product_id: str
    description: str
    actions: tuple[str, ...] = ()


@dataclass(frozen=True)
class OrderChange:
    """退款或改签等订单变更，只追加、不改写原始条目。"""

    change_type: OrderChangeType
    line_id: str | None
    note: str
    occurred_at: datetime


@dataclass(frozen=True)
class ConsumerOrder:
    order_id: str
    consumer_id: str
    placed_at: datetime
    lines: tuple[OrderLine, ...]


@dataclass(frozen=True)
class DisputeItem:
    """消费者的一项主张，锚定到订单条目与绿色行动。"""

    item_id: str
    line_id: str
    action: str
    assertion: str


@dataclass(frozen=True)
class EvidenceItem:
    evidence_id: str
    case_id: str
    kind: EvidenceKind
    material_class: MaterialClass
    submitted_by: ActorRole
    actor_id: str
    content_hash: str
    payload: Mapping[str, Any]
    consumer_original: bool
    received_at: datetime


@dataclass(frozen=True)
class Acceptance:
    """凭证受理结果，重复上传时原样返回。"""

    acceptance_id: str
    evidence_id: str
    case_id: str
    content_hash: str
    accepted_at: datetime


@dataclass(frozen=True)
class IntakeResult:
    outcome: IntakeOutcome
    evidence_id: str
    acceptance_id: str | None
    accepted_at: datetime | None


@dataclass(frozen=True)
class ConflictRecord:
    """同一凭证出现内容冲突的记录，冻结争议而非任选一份。"""

    evidence_id: str
    existing_hash: str
    incoming_hash: str
    actor_id: str
    at: datetime


@dataclass(frozen=True)
class ItemDecision:
    item_id: str
    upheld: bool
    mediator_id: str
    rationale: str
    decided_at: datetime


@dataclass(frozen=True)
class Remedy:
    remedy_type: RemedyType
    detail: str
    enforceable: bool
    confirmed_by: str
    confirmed_at: datetime


@dataclass(frozen=True)
class TrailEntry:
    """处理轨迹：谁在何时做了什么。"""

    actor: str
    action: str
    at: datetime


@dataclass(frozen=True)
class GreenCredit:
    """计入绿色行动的一个订单条目。"""

    line_id: str
    category: LineCategory
    action: str
    rule_version: int
    claim_version: int


@dataclass(frozen=True)
class ItemExplanation:
    """单项主张的说明：采用的规则与承诺版本、经手人、结论。"""

    item_id: str
    line_id: str
    action: str
    assertion: str
    rule_version: int
    claim_version: int
    handlers: tuple[str, ...]
    upheld: bool | None
    rationale: str | None


@dataclass(frozen=True)
class CaseExplanation:
    case_id: str
    status: CaseStatus
    claim_summary: str
    items: tuple[ItemExplanation, ...]
    remedy: Remedy | None
    enforceable_remedy: bool


def evaluate_green_actions(
    order: ConsumerOrder, claim: ClaimVersion, rules: RuleVersion
) -> tuple[GreenCredit, ...]:
    """只把同时被承诺版本与规则版本认可的条目计入绿色行动。

    一次订单可含餐饮、礼盒、出行权益；条目需落在承诺适用时间与
    适用商品内，且行动同时出现在承诺与规则中，才计入。
    """
    if not (claim.window_start <= order.placed_at <= claim.window_end):
        return ()
    promised = {(p.category, p.action) for p in claim.promised_actions}
    recognized = {(r.category, r.action) for r in rules.rules}
    credits: list[GreenCredit] = []
    for line in order.lines:
        if line.product_id not in claim.applicable_products:
            continue
        for action in line.actions:
            key = (line.category, action)
            if key in promised and key in recognized:
                credits.append(
                    GreenCredit(
                        line_id=line.line_id,
                        category=line.category,
                        action=action,
                        rule_version=rules.version,
                        claim_version=claim.version,
                    )
                )
    return tuple(credits)


PII_FIELDS = frozenset({"consumer_name", "phone", "address", "id_number", "account"})


def mask_pii(payload: Mapping[str, Any]) -> dict[str, Any]:
    """对个人信息字段脱敏，供调解员视图等最小可见场景使用。"""
    masked: dict[str, Any] = {}
    for key, value in payload.items():
        if key in PII_FIELDS and isinstance(value, str) and value:
            masked[key] = _mask_value(key, value)
        else:
            masked[key] = value
    return masked


def _mask_value(key: str, value: str) -> str:
    if key == "consumer_name":
        return value[0] + "*" * (len(value) - 1)
    if key == "phone":
        return value[:3] + "****" + value[-2:] if len(value) >= 6 else "***"
    if key == "id_number":
        return value[:3] + "********" + value[-2:] if len(value) >= 6 else "***"
    if key == "address":
        return value[:6] + "***" if len(value) > 6 else "***"
    return "***"
