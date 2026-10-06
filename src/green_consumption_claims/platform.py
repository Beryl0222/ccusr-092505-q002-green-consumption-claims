"""绿色消费凭证争议台的领域核心：受理、冻结、调解与可解释性。

设计要点：
- 所有状态变更先写成领域事件（信封契约见 contracts/domain.schema.json），
  内存状态只是事件重放的结果；平台重启后用同一事件存储重新构造本对象，
  已保存的受理结果与期限即可继续。
- 向外部投诉渠道的同步是旁路的：渠道暂不可用时事件留在本地待同步，
  不影响受理与期限。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Protocol

from .contracts import validate_event
from .domain import (
    SCOPE_OPEN_CASE,
    SCOPE_ORDER_EVIDENCE,
    Acceptance,
    ActorRole,
    Authorization,
    CaseExplanation,
    CaseStatus,
    ClaimVersion,
    ConflictRecord,
    ConsumerOrder,
    DisputeItem,
    EvidenceItem,
    EvidenceKind,
    GreenCredit,
    GreenRule,
    IntakeOutcome,
    IntakeResult,
    ItemDecision,
    ItemExplanation,
    LineCategory,
    MaterialClass,
    OrderChange,
    OrderChangeType,
    OrderLine,
    PromisedAction,
    Remedy,
    RemedyType,
    RuleVersion,
    TrailEntry,
    evaluate_green_actions,
    mask_pii,
)
from .store import EventStore


class DomainError(Exception):
    """领域规则被拒绝。"""


class NotFound(DomainError):
    """引用的对象不存在。"""


class PermissionDenied(DomainError):
    """越权操作。"""


class InvalidState(DomainError):
    """当前状态不允许该操作。"""


class ChannelUnavailable(DomainError):
    """外部投诉渠道暂不可用。"""


class ExternalChannel(Protocol):
    """外部投诉渠道：平台把已保存的事件推送过去。"""

    def push(self, event: Mapping[str, Any]) -> None: ...


RESPONSE_WINDOW = timedelta(days=7)
RULE_BOOK_ID = "green-action-rules"


@dataclass
class _ClaimState:
    merchant_id: str
    versions: list[ClaimVersion] = field(default_factory=list)


@dataclass
class _OrderState:
    order: ConsumerOrder
    changes: list[OrderChange] = field(default_factory=list)


@dataclass
class _CaseState:
    case_id: str
    order_id: str
    claim_id: str
    claim_version: int
    rule_version: int
    opened_by: str
    opened_at: datetime
    response_due_at: datetime
    status: CaseStatus = CaseStatus.OPEN
    items: dict[str, DisputeItem] = field(default_factory=dict)
    decisions: dict[str, ItemDecision] = field(default_factory=dict)
    conflicts: list[ConflictRecord] = field(default_factory=list)
    trail: list[TrailEntry] = field(default_factory=list)
    remedy: Remedy | None = None


class DisputePlatform:
    """绿色消费凭证争议台。

    用同一事件存储重新构造本对象即完成重启恢复；所有命令时间
    由调用方传入且必须带时区，期限只由已保存的事实推导。
    """

    def __init__(
        self,
        store: EventStore,
        *,
        schema: Mapping[str, Any] | None = None,
        sync_marker_path: str | Path | None = None,
    ) -> None:
        self._store = store
        self._schema = schema
        self._sync_marker_path = Path(sync_marker_path) if sync_marker_path else None
        self._rules: dict[int, RuleVersion] = {}
        self._claims: dict[str, _ClaimState] = {}
        self._orders: dict[str, _OrderState] = {}
        self._cases: dict[str, _CaseState] = {}
        self._evidence: dict[str, EvidenceItem] = {}
        self._acceptances: dict[str, Acceptance] = {}
        self._events: list[dict[str, Any]] = []
        self._agg_versions: dict[str, int] = {}
        for event in store.load():
            self._apply(event)
            self._events.append(event)
        self._synced = self._read_sync_marker()

    # ------------------------------------------------------------------
    # 命令：规则与商家承诺（版本化，只增不改）
    # ------------------------------------------------------------------

    def publish_rules(
        self,
        *,
        version: int,
        rules: list[GreenRule],
        issued_at: datetime,
        at: datetime,
        published_by: str = "system",
    ) -> RuleVersion:
        """发布新版绿色行动规则；已有版本不可改写，更新只用于新案件。"""
        _require_positive_int(version, "version")
        _require_aware(issued_at, "issued_at")
        _require_aware(at, "at")
        if self._rules and version <= max(self._rules):
            raise InvalidState("规则版本必须递增，已有版本不可改写")
        if not rules:
            raise DomainError("规则版本至少包含一条规则")
        payload = {
            "version": version,
            "issued_at": issued_at.isoformat(),
            "published_by": published_by,
            "rules": [
                {"category": r.category.value, "action": r.action, "description": r.description}
                for r in rules
            ],
        }
        self._emit(
            event_type="RULE_PUBLISHED",
            aggregate_type="rule_book",
            aggregate_id=RULE_BOOK_ID,
            occurred_at=at,
            summary=f"发布绿色行动规则v{version}",
            payload=payload,
        )
        return self._rules[version]

    def publish_claim(
        self,
        *,
        merchant_id: str,
        claim_id: str,
        version: int,
        window_start: datetime,
        window_end: datetime,
        applicable_products: frozenset[str],
        promised_actions: list[PromisedAction],
        summary: str,
        at: datetime,
    ) -> ClaimVersion:
        """发布商家承诺的一个版本；历史版本保留，下架不等于抹除。"""
        _require_positive_int(version, "version")
        for value, field_name in ((window_start, "window_start"), (window_end, "window_end"), (at, "at")):
            _require_aware(value, field_name)
        if not window_start < window_end:
            raise DomainError("承诺适用时间的起点必须早于终点")
        if not summary.strip():
            raise DomainError("承诺摘要不能为空")
        state = self._claims.get(claim_id)
        if state is not None:
            if state.merchant_id != merchant_id:
                raise PermissionDenied("只能由承诺所属商家发布新版本")
            if version <= max(v.version for v in state.versions):
                raise InvalidState("承诺版本必须递增，历史版本不可改写")
        payload = {
            "merchant_id": merchant_id,
            "version": version,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "applicable_products": sorted(applicable_products),
            "promised_actions": [
                {"category": a.category.value, "action": a.action, "detail": a.detail}
                for a in promised_actions
            ],
            "summary": summary,
        }
        self._emit(
            event_type="CLAIM_PUBLISHED",
            aggregate_type="merchant_claim",
            aggregate_id=claim_id,
            occurred_at=at,
            summary=f"商家承诺v{version}已发布",
            payload=payload,
        )
        return self._claim_version(claim_id, version)

    # ------------------------------------------------------------------
    # 命令：订单登记与变更
    # ------------------------------------------------------------------

    def register_order(self, *, order: ConsumerOrder, authorization: Authorization, at: datetime) -> None:
        """登记消费者授权的订单快照。"""
        if order.order_id in self._orders:
            raise InvalidState("订单已登记")
        _require_aware(order.placed_at, "order.placed_at")
        _require_aware(at, "at")
        if not authorization.allows(SCOPE_ORDER_EVIDENCE, order.consumer_id):
            raise PermissionDenied("订单证据需要消费者本人授权")
        if not order.lines:
            raise DomainError("订单至少包含一个条目")
        line_ids = [line.line_id for line in order.lines]
        if len(set(line_ids)) != len(line_ids):
            raise DomainError("订单条目编号重复")
        payload = {
            "consumer_id": order.consumer_id,
            "placed_at": order.placed_at.isoformat(),
            "lines": [
                {
                    "line_id": line.line_id,
                    "category": line.category.value,
                    "product_id": line.product_id,
                    "description": line.description,
                    "actions": list(line.actions),
                }
                for line in order.lines
            ],
        }
        self._emit(
            event_type="ORDER_RECORDED",
            aggregate_type="consumer_order",
            aggregate_id=order.order_id,
            occurred_at=at,
            summary="登记消费者授权的订单",
            payload=payload,
        )

    def record_order_change(
        self,
        *,
        order_id: str,
        change_type: OrderChangeType,
        line_id: str | None = None,
        note: str = "",
        at: datetime,
    ) -> None:
        """记录退款或改签；只追加变更，不抹掉原承诺与已计入的绿色行动。"""
        state = self._order(order_id)
        _require_aware(at, "at")
        if line_id is not None and line_id not in {line.line_id for line in state.order.lines}:
            raise NotFound(f"订单条目不存在: {line_id}")
        self._emit(
            event_type="ORDER_AMENDED",
            aggregate_type="consumer_order",
            aggregate_id=order_id,
            occurred_at=at,
            summary=f"订单发生{change_type.value}，原承诺保留",
            payload={"change_type": change_type.value, "line_id": line_id, "note": note},
        )

    # ------------------------------------------------------------------
    # 命令：立案、凭证受理、调解
    # ------------------------------------------------------------------

    def open_case(
        self,
        *,
        case_id: str,
        order_id: str,
        claim_id: str,
        items: list[DisputeItem],
        opened_by: str,
        authorization: Authorization,
        at: datetime,
    ) -> datetime:
        """立案：绑定订单时间适用的承诺版本与立案时的规则版本，返回响应期限。"""
        if case_id in self._cases:
            raise InvalidState("案件编号已存在")
        _require_aware(at, "at")
        order_state = self._order(order_id)
        claim_state = self._claims.get(claim_id)
        if claim_state is None:
            raise NotFound(f"商家承诺不存在: {claim_id}")
        if not authorization.allows(SCOPE_OPEN_CASE, order_state.order.consumer_id):
            raise PermissionDenied("立案需要消费者本人授权")
        if not self._rules:
            raise InvalidState("尚未发布绿色行动规则，无法立案")
        claim_version = self._claim_version_at(claim_state, order_state.order.placed_at)
        if claim_version is None:
            raise InvalidState("订单时间之前没有已发布的承诺版本，无法确定适用承诺")
        if not items:
            raise DomainError("至少登记一项主张")
        item_ids = [item.item_id for item in items]
        if len(set(item_ids)) != len(item_ids):
            raise DomainError("主张编号重复")
        known_lines = {line.line_id for line in order_state.order.lines}
        for item in items:
            if item.line_id not in known_lines:
                raise NotFound(f"主张引用的订单条目不存在: {item.line_id}")
        rule_version = max(self._rules)
        due = at + RESPONSE_WINDOW
        payload = {
            "order_id": order_id,
            "claim_id": claim_id,
            "claim_version": claim_version.version,
            "rule_version": rule_version,
            "opened_by": opened_by,
            "response_due_at": due.isoformat(),
            "items": [
                {
                    "item_id": item.item_id,
                    "line_id": item.line_id,
                    "action": item.action,
                    "assertion": item.assertion,
                }
                for item in items
            ],
        }
        self._emit(
            event_type="CASE_OPENED",
            aggregate_type="mediation_case",
            aggregate_id=case_id,
            occurred_at=at,
            summary="立案受理",
            payload=payload,
        )
        return due

    def submit_evidence(
        self,
        *,
        case_id: str,
        evidence_id: str,
        kind: EvidenceKind,
        material_class: MaterialClass,
        submitted_by: ActorRole,
        actor_id: str,
        content: str | bytes,
        payload: Mapping[str, Any],
        consumer_original: bool = False,
        authorization: Authorization | None = None,
        at: datetime,
    ) -> IntakeResult:
        """受理凭证：重复上传返回既有受理结果，内容冲突冻结争议。"""
        case = self._case(case_id)
        _require_aware(at, "at")
        if case.status is CaseStatus.REMEDY_CONFIRMED:
            raise InvalidState("案件已确认补偿或整改，不再接收凭证")
        if material_class is MaterialClass.MEDIATION_CONCLUSION and submitted_by not in (
            ActorRole.MEDIATOR,
            ActorRole.SYSTEM,
        ):
            raise PermissionDenied("调解结论只能由调解员登记")
        if consumer_original and submitted_by is not ActorRole.CONSUMER:
            raise PermissionDenied("消费者原始凭证只能由消费者本人提交")
        order = self._orders[case.order_id].order
        if kind is EvidenceKind.ORDER_RECORD and (
            authorization is None or not authorization.allows(SCOPE_ORDER_EVIDENCE, order.consumer_id)
        ):
            raise PermissionDenied("订单证据需要消费者本人授权")
        raw = content.encode("utf-8") if isinstance(content, str) else content
        content_hash = hashlib.sha256(raw).hexdigest()
        existing = self._evidence.get(evidence_id)
        if existing is not None:
            if existing.content_hash == content_hash:
                acceptance = self._acceptances[evidence_id]
                return IntakeResult(
                    IntakeOutcome.DUPLICATE, evidence_id, acceptance.acceptance_id, acceptance.accepted_at
                )
            if existing.consumer_original and submitted_by is ActorRole.MERCHANT:
                raise PermissionDenied("商家不能修改消费者原始凭证")
            # 内容冲突：保留原有凭证，冻结争议，不任选一份
            self._emit(
                event_type="CASE_FROZEN",
                aggregate_type="mediation_case",
                aggregate_id=existing.case_id,
                occurred_at=at,
                summary="凭证内容冲突，冻结争议",
                payload={
                    "evidence_id": evidence_id,
                    "existing_hash": existing.content_hash,
                    "incoming_hash": content_hash,
                    "actor_id": actor_id,
                },
            )
            return IntakeResult(IntakeOutcome.CONFLICT_FROZEN, evidence_id, None, None)
        self._emit(
            event_type="EVIDENCE_RECEIVED",
            aggregate_type="evidence_item",
            aggregate_id=evidence_id,
            occurred_at=at,
            summary="受理凭证",
            payload={
                "case_id": case_id,
                "kind": kind.value,
                "material_class": material_class.value,
                "submitted_by": submitted_by.value,
                "actor_id": actor_id,
                "content_hash": content_hash,
                "consumer_original": consumer_original,
                "payload": dict(payload),
            },
        )
        acceptance = self._acceptances[evidence_id]
        return IntakeResult(IntakeOutcome.ACCEPTED, evidence_id, acceptance.acceptance_id, acceptance.accepted_at)

    def decide_item(
        self,
        *,
        case_id: str,
        item_id: str,
        upheld: bool,
        mediator_id: str,
        rationale: str,
        at: datetime,
    ) -> None:
        """调解员对一项主张给出结论；冻结或已结案件不可登记。"""
        case = self._case(case_id)
        _require_aware(at, "at")
        if not isinstance(upheld, bool):
            raise DomainError("结论必须是布尔值")
        if case.status is CaseStatus.FROZEN:
            raise InvalidState("案件已冻结，需先处理凭证冲突")
        if case.status is CaseStatus.REMEDY_CONFIRMED:
            raise InvalidState("案件已结案")
        if item_id not in case.items:
            raise NotFound(f"主张不存在: {item_id}")
        if item_id in case.decisions:
            raise InvalidState("该主张已有结论")
        self._emit(
            event_type="MEDIATION_DECIDED",
            aggregate_type="mediation_case",
            aggregate_id=case_id,
            occurred_at=at,
            summary=f"登记调解决定：{item_id}",
            payload={
                "item_id": item_id,
                "upheld": upheld,
                "mediator_id": mediator_id,
                "rationale": rationale,
            },
        )

    def confirm_remedy(
        self,
        *,
        case_id: str,
        remedy_type: RemedyType,
        detail: str,
        enforceable: bool,
        confirmed_by: str,
        at: datetime,
    ) -> None:
        """全部主张有结论后，确认是否形成可执行的补偿或整改。"""
        case = self._case(case_id)
        _require_aware(at, "at")
        if not isinstance(enforceable, bool):
            raise DomainError("可执行标记必须是布尔值")
        if case.status is not CaseStatus.OPEN:
            raise InvalidState("只有进行中的案件可以确认补偿或整改")
        undecided = [item_id for item_id in case.items if item_id not in case.decisions]
        if undecided:
            raise InvalidState(f"尚有主张未结论，不能确认补偿或整改: {undecided}")
        self._emit(
            event_type="REMEDY_CONFIRMED",
            aggregate_type="mediation_case",
            aggregate_id=case_id,
            occurred_at=at,
            summary="确认补偿或整改",
            payload={
                "remedy_type": remedy_type.value,
                "detail": detail,
                "enforceable": enforceable,
                "confirmed_by": confirmed_by,
            },
        )

    # ------------------------------------------------------------------
    # 查询：绿色行动计入、期限、调解员视图、可解释性
    # ------------------------------------------------------------------

    def green_credits(self, case_id: str) -> tuple[GreenCredit, ...]:
        """按案件绑定的承诺版本与规则版本，计入符合条件的订单条目。"""
        case = self._case(case_id)
        order = self._orders[case.order_id].order
        claim = self._claim_version(case.claim_id, case.claim_version)
        rules = self._rules[case.rule_version]
        return evaluate_green_actions(order, claim, rules)

    def case_deadline(self, case_id: str) -> datetime:
        return self._case(case_id).response_due_at

    def case_status(self, case_id: str) -> CaseStatus:
        return self._case(case_id).status

    def overdue_cases(self, now: datetime) -> tuple[tuple[str, datetime], ...]:
        """已逾响应期限的进行中案件，按期限升序。"""
        _require_aware(now, "now")
        overdue = [
            (case.case_id, case.response_due_at)
            for case in self._cases.values()
            if case.status is CaseStatus.OPEN and case.response_due_at < now
        ]
        return tuple(sorted(overdue, key=lambda entry: (entry[1], entry[0])))

    def get_acceptance(self, evidence_id: str) -> Acceptance | None:
        return self._acceptances.get(evidence_id)

    def get_evidence(self, evidence_id: str) -> EvidenceItem | None:
        return self._evidence.get(evidence_id)

    def order_changes(self, order_id: str) -> tuple[OrderChange, ...]:
        return tuple(self._order(order_id).changes)

    def mediator_view(self, case_id: str) -> dict[str, Any]:
        """调解员视图：只呈现必要信息，个人信息字段脱敏。"""
        case = self._case(case_id)
        evidence = sorted(
            (item for item in self._evidence.values() if item.case_id == case_id),
            key=lambda item: (item.received_at, item.evidence_id),
        )
        return {
            "case_id": case.case_id,
            "status": case.status.value,
            "rule_version": case.rule_version,
            "claim_version": case.claim_version,
            "opened_at": case.opened_at.isoformat(),
            "response_due_at": case.response_due_at.isoformat(),
            "items": [
                {
                    "item_id": item.item_id,
                    "line_id": item.line_id,
                    "action": item.action,
                    "assertion": item.assertion,
                }
                for item in case.items.values()
            ],
            "evidence": [
                {
                    "evidence_id": item.evidence_id,
                    "kind": item.kind.value,
                    "material_class": item.material_class.value,
                    "submitted_by": item.submitted_by.value,
                    "received_at": item.received_at.isoformat(),
                    "payload": mask_pii(item.payload),
                }
                for item in evidence
            ],
            "trail": [
                {"actor": entry.actor, "action": entry.action, "at": entry.at.isoformat()}
                for entry in case.trail
            ],
        }

    def explain_case(self, case_id: str) -> CaseExplanation:
        """说明每项主张采用的规则与承诺版本、经手人、结论与可执行结果。"""
        case = self._case(case_id)
        claim = self._claim_version(case.claim_id, case.claim_version)
        items: list[ItemExplanation] = []
        for item_id, item in case.items.items():
            decision = case.decisions.get(item_id)
            handlers = [case.opened_by]
            if decision is not None:
                handlers.append(decision.mediator_id)
            items.append(
                ItemExplanation(
                    item_id=item.item_id,
                    line_id=item.line_id,
                    action=item.action,
                    assertion=item.assertion,
                    rule_version=case.rule_version,
                    claim_version=case.claim_version,
                    handlers=tuple(handlers),
                    upheld=decision.upheld if decision else None,
                    rationale=decision.rationale if decision else None,
                )
            )
        return CaseExplanation(
            case_id=case.case_id,
            status=case.status,
            claim_summary=claim.summary,
            items=tuple(items),
            remedy=case.remedy,
            enforceable_remedy=bool(case.remedy and case.remedy.enforceable),
        )

    # ------------------------------------------------------------------
    # 外部投诉渠道同步（旁路，不影响本地受理与期限）
    # ------------------------------------------------------------------

    def pending_external_count(self) -> int:
        return len(self._events) - self._synced

    def sync_external(self, channel: ExternalChannel) -> int:
        """把未同步的事件推给外部投诉渠道；渠道暂不可用则保留待同步。"""
        pushed = 0
        for event in self._events[self._synced :]:
            try:
                channel.push(event)
            except ChannelUnavailable:
                break
            self._synced += 1
            pushed += 1
            self._write_sync_marker()
        return pushed

    # ------------------------------------------------------------------
    # 内部：事件落盘与重放
    # ------------------------------------------------------------------

    def _emit(
        self,
        *,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        occurred_at: datetime,
        summary: str,
        payload: Mapping[str, Any],
    ) -> None:
        key = f"{aggregate_type}:{aggregate_id}"
        event = {
            "event_id": f"EV-{len(self._events) + 1:06d}",
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "occurred_at": occurred_at.isoformat(),
            "version": self._agg_versions.get(key, 0) + 1,
            "summary": summary,
            "payload": dict(payload),
        }
        if self._schema is not None:
            issues = validate_event(event, self._schema)
            if issues:
                raise DomainError(
                    "事件不符合契约: " + "; ".join(f"{issue.field}:{issue.code}" for issue in issues)
                )
        self._store.append(event)
        self._apply(event)
        self._events.append(event)

    def _apply(self, event: Mapping[str, Any]) -> None:
        key = f"{event['aggregate_type']}:{event['aggregate_id']}"
        self._agg_versions[key] = self._agg_versions.get(key, 0) + 1
        event_type = event["event_type"]
        aggregate_id = event["aggregate_id"]
        payload = event.get("payload", {})
        at = datetime.fromisoformat(event["occurred_at"])
        if event_type == "RULE_PUBLISHED":
            self._rules[payload["version"]] = RuleVersion(
                version=payload["version"],
                issued_at=datetime.fromisoformat(payload["issued_at"]),
                rules=tuple(
                    GreenRule(LineCategory(r["category"]), r["action"], r["description"])
                    for r in payload["rules"]
                ),
            )
        elif event_type == "CLAIM_PUBLISHED":
            state = self._claims.setdefault(aggregate_id, _ClaimState(merchant_id=payload["merchant_id"]))
            state.versions.append(
                ClaimVersion(
                    version=payload["version"],
                    published_at=at,
                    window_start=datetime.fromisoformat(payload["window_start"]),
                    window_end=datetime.fromisoformat(payload["window_end"]),
                    applicable_products=frozenset(payload["applicable_products"]),
                    promised_actions=tuple(
                        PromisedAction(LineCategory(a["category"]), a["action"], a["detail"])
                        for a in payload["promised_actions"]
                    ),
                    summary=payload["summary"],
                )
            )
        elif event_type == "ORDER_RECORDED":
            self._orders[aggregate_id] = _OrderState(
                order=ConsumerOrder(
                    order_id=aggregate_id,
                    consumer_id=payload["consumer_id"],
                    placed_at=datetime.fromisoformat(payload["placed_at"]),
                    lines=tuple(
                        OrderLine(
                            line["line_id"],
                            LineCategory(line["category"]),
                            line["product_id"],
                            line["description"],
                            tuple(line["actions"]),
                        )
                        for line in payload["lines"]
                    ),
                )
            )
        elif event_type == "ORDER_AMENDED":
            self._orders[aggregate_id].changes.append(
                OrderChange(OrderChangeType(payload["change_type"]), payload["line_id"], payload["note"], at)
            )
        elif event_type == "CASE_OPENED":
            self._cases[aggregate_id] = _CaseState(
                case_id=aggregate_id,
                order_id=payload["order_id"],
                claim_id=payload["claim_id"],
                claim_version=payload["claim_version"],
                rule_version=payload["rule_version"],
                opened_by=payload["opened_by"],
                opened_at=at,
                response_due_at=datetime.fromisoformat(payload["response_due_at"]),
                items={
                    item["item_id"]: DisputeItem(
                        item["item_id"], item["line_id"], item["action"], item["assertion"]
                    )
                    for item in payload["items"]
                },
                trail=[TrailEntry(payload["opened_by"], "立案受理", at)],
            )
        elif event_type == "EVIDENCE_RECEIVED":
            self._evidence[aggregate_id] = EvidenceItem(
                evidence_id=aggregate_id,
                case_id=payload["case_id"],
                kind=EvidenceKind(payload["kind"]),
                material_class=MaterialClass(payload["material_class"]),
                submitted_by=ActorRole(payload["submitted_by"]),
                actor_id=payload["actor_id"],
                content_hash=payload["content_hash"],
                payload=dict(payload["payload"]),
                consumer_original=payload["consumer_original"],
                received_at=at,
            )
            self._acceptances[aggregate_id] = Acceptance(
                acceptance_id=f"ACC-{aggregate_id}",
                evidence_id=aggregate_id,
                case_id=payload["case_id"],
                content_hash=payload["content_hash"],
                accepted_at=at,
            )
        elif event_type == "CASE_FROZEN":
            case = self._cases[aggregate_id]
            case.status = CaseStatus.FROZEN
            case.conflicts.append(
                ConflictRecord(
                    payload["evidence_id"], payload["existing_hash"], payload["incoming_hash"], payload["actor_id"], at
                )
            )
            case.trail.append(TrailEntry(payload["actor_id"], "凭证内容冲突，冻结争议", at))
        elif event_type == "MEDIATION_DECIDED":
            case = self._cases[aggregate_id]
            case.decisions[payload["item_id"]] = ItemDecision(
                payload["item_id"], payload["upheld"], payload["mediator_id"], payload["rationale"], at
            )
            case.trail.append(TrailEntry(payload["mediator_id"], f"登记调解决定：{payload['item_id']}", at))
        elif event_type == "REMEDY_CONFIRMED":
            case = self._cases[aggregate_id]
            case.remedy = Remedy(
                RemedyType(payload["remedy_type"]),
                payload["detail"],
                payload["enforceable"],
                payload["confirmed_by"],
                at,
            )
            case.status = CaseStatus.REMEDY_CONFIRMED
            case.trail.append(TrailEntry(payload["confirmed_by"], "确认补偿或整改", at))
        else:
            raise DomainError(f"未登记的事件类型: {event_type}")

    # ------------------------------------------------------------------
    # 内部：辅助
    # ------------------------------------------------------------------

    def _order(self, order_id: str) -> _OrderState:
        try:
            return self._orders[order_id]
        except KeyError:
            raise NotFound(f"订单不存在: {order_id}") from None

    def _case(self, case_id: str) -> _CaseState:
        try:
            return self._cases[case_id]
        except KeyError:
            raise NotFound(f"案件不存在: {case_id}") from None

    def _claim_version(self, claim_id: str, version: int) -> ClaimVersion:
        state = self._claims.get(claim_id)
        if state is None:
            raise NotFound(f"商家承诺不存在: {claim_id}")
        for claim_version in state.versions:
            if claim_version.version == version:
                return claim_version
        raise NotFound(f"商家承诺版本不存在: {claim_id} v{version}")

    @staticmethod
    def _claim_version_at(state: _ClaimState, at: datetime) -> ClaimVersion | None:
        candidates = [version for version in state.versions if version.published_at <= at]
        return max(candidates, key=lambda version: version.version, default=None)

    def _read_sync_marker(self) -> int:
        if self._sync_marker_path and self._sync_marker_path.exists():
            text = self._sync_marker_path.read_text(encoding="utf-8").strip()
            return int(text) if text else 0
        return 0

    def _write_sync_marker(self) -> None:
        if self._sync_marker_path:
            self._sync_marker_path.parent.mkdir(parents=True, exist_ok=True)
            self._sync_marker_path.write_text(str(self._synced), encoding="utf-8")


def _require_aware(value: datetime, field_name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise DomainError(f"{field_name} 必须包含时区")


def _require_positive_int(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise DomainError(f"{field_name} 必须是正整数")
