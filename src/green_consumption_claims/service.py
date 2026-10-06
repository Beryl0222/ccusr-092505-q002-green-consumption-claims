"""绿色消费凭证争议台的核心服务。

职责边界：
- 商家承诺按版本登记，下架不抹除，退款/改签不改写承诺原文；
- 绿色行动规则按版本生效，案件立案时锁定规则版本，规则更新只用于新案件；
- 凭证全局幂等：同一凭证重复上传返回既有受理回执，内容冲突冻结争议而不是任选一份；
- 凭证按上传方分级（事实材料 / 商家自证 / 调解结论），商家不能改写消费者原始凭证；
- 所有状态落盘，重启或外部投诉渠道不可用时，已保存的受理与期限继续有效；
- 调解员视图对个人信息做最小化处理；
- 每项主张可回溯所用规则版本、经手人与是否形成可执行的补偿或整改。
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .models import (
    ORDER_EVIDENCE_KIND,
    TERMINAL_STATUSES,
    UPLOADER_CLASSIFICATION,
    AuthorizationError,
    CaseStatus,
    DisputeError,
    EvidenceClass,
    EvidenceConflictError,
    GreenCategory,
    NotFoundError,
    RemedyKind,
    StateError,
    TamperError,
    content_digest,
    iso,
    mask_contact,
    parse_time,
    pseudonym,
    require_aware,
)
from .store import JsonStore

_VALID_CATEGORIES = {item.value for item in GreenCategory}
_VALID_CHANGES = {"refunded", "rebooked"}


class DisputeDesk:
    """争议台门面：所有写操作即时落盘，读操作返回深拷贝。"""

    def __init__(
        self,
        store_path: str | Path,
        *,
        response_window: timedelta = timedelta(days=7),
        decide_window: timedelta = timedelta(days=15),
    ) -> None:
        self._store = JsonStore(store_path)
        self._state = self._store.load()
        self._response_window = response_window
        self._decide_window = decide_window

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _persist(self) -> None:
        self._store.save(self._state)

    def _next_id(self, prefix: str) -> str:
        self._state["seq"] += 1
        return f"{prefix}-{self._state['seq']:06d}"

    def _audit(
        self,
        actor: str,
        action: str,
        now: datetime,
        *,
        case_id: str | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        entry = {
            "seq": self._state["seq"],
            "at": iso(now),
            "actor": actor,
            "action": action,
            "case_id": case_id,
            "detail": dict(detail or {}),
        }
        self._state["audit"].append(entry)

    def _get(self, bucket: str, key: str) -> dict[str, Any]:
        try:
            return self._state[bucket][key]
        except KeyError:
            raise NotFoundError(f"{bucket} 中不存在 {key}") from None

    def _get_case(self, case_id: str) -> dict[str, Any]:
        return self._get("cases", case_id)

    # ------------------------------------------------------------------
    # 商家承诺：版本化登记，下架不抹除
    # ------------------------------------------------------------------

    def publish_claim(
        self,
        *,
        claim_id: str,
        merchant_id: str,
        version: int,
        content: str,
        categories: Iterable[str],
        valid_from: datetime,
        valid_to: datetime,
        now: datetime,
    ) -> dict[str, Any]:
        """登记一版商家承诺。版本必须为正整数且单调递增。"""
        require_aware(valid_from, "valid_from")
        require_aware(valid_to, "valid_to")
        require_aware(now, "now")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise DisputeError("承诺版本必须是正整数")
        if valid_from > valid_to:
            raise DisputeError("承诺有效期起止颠倒")
        category_list = [str(item) for item in categories]
        unknown = sorted(set(category_list) - _VALID_CATEGORIES)
        if unknown:
            raise DisputeError(f"未登记的绿色品类: {unknown}")
        versions = self._state["claims"].setdefault(claim_id, {})
        if str(version) in versions or (
            versions and version <= max(int(item) for item in versions)
        ):
            raise StateError("承诺版本必须单调递增，已登记的版本不可改写")
        claim = {
            "claim_id": claim_id,
            "merchant_id": merchant_id,
            "version": version,
            "content": content,
            "categories": category_list,
            "valid_from": iso(valid_from),
            "valid_to": iso(valid_to),
            "published_at": iso(now),
            "delisted_at": None,
        }
        versions[str(version)] = claim
        self._state["seq"] += 1
        self._audit(merchant_id, "claim_published", now, detail={"claim_id": claim_id, "version": version})
        self._persist()
        return copy.deepcopy(claim)

    def delist_claim(self, claim_id: str, *, merchant_id: str, now: datetime) -> None:
        """商家下架活动：只标记下架时间，承诺原文与历史版本保留可查。"""
        require_aware(now, "now")
        versions = self._state["claims"].get(claim_id)
        if not versions:
            raise NotFoundError(f"claims 中不存在 {claim_id}")
        for claim in versions.values():
            if claim["delisted_at"] is None:
                claim["delisted_at"] = iso(now)
        self._state["seq"] += 1
        self._audit(merchant_id, "claim_delisted", now, detail={"claim_id": claim_id})
        self._persist()

    def get_claim(self, claim_id: str, *, version: int | None = None) -> dict[str, Any]:
        versions = self._state["claims"].get(claim_id)
        if not versions:
            raise NotFoundError(f"claims 中不存在 {claim_id}")
        if version is None:
            version = max(int(item) for item in versions)
        try:
            return copy.deepcopy(versions[str(version)])
        except KeyError:
            raise NotFoundError(f"承诺 {claim_id} 没有版本 {version}") from None

    # ------------------------------------------------------------------
    # 绿色行动规则：按版本生效，只约束新案件
    # ------------------------------------------------------------------

    def register_rules(
        self,
        *,
        version: int,
        effective_from: datetime,
        criteria: Mapping[str, Mapping[str, Any]],
        now: datetime,
        actor: str = "platform",
    ) -> dict[str, Any]:
        """登记一版绿色行动认定规则。criteria 按品类给出属性要求。"""
        require_aware(effective_from, "effective_from")
        require_aware(now, "now")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise DisputeError("规则版本必须是正整数")
        existing = [item["version"] for item in self._state["rules"]]
        if existing and version <= max(existing):
            raise StateError("规则版本必须单调递增，旧版本不可改写")
        unknown = sorted(set(criteria) - _VALID_CATEGORIES)
        if unknown:
            raise DisputeError(f"未登记的绿色品类: {unknown}")
        ruleset = {
            "version": version,
            "effective_from": iso(effective_from),
            "criteria": copy.deepcopy(dict(criteria)),
            "registered_at": iso(now),
        }
        self._state["rules"].append(ruleset)
        self._state["rules"].sort(key=lambda item: item["version"])
        self._state["seq"] += 1
        self._audit(actor, "rules_registered", now, detail={"version": version})
        self._persist()
        return copy.deepcopy(ruleset)

    def _ruleset_at(self, moment: datetime) -> dict[str, Any]:
        chosen: dict[str, Any] | None = None
        for ruleset in self._state["rules"]:
            if parse_time(ruleset["effective_from"]) <= moment:
                chosen = ruleset
        if chosen is None:
            raise StateError("立案时间之前没有已生效的绿色行动规则")
        return chosen

    def _ruleset_by_version(self, version: int) -> dict[str, Any]:
        for ruleset in self._state["rules"]:
            if ruleset["version"] == version:
                return ruleset
        raise NotFoundError(f"没有规则版本 {version}")

    # ------------------------------------------------------------------
    # 订单：一次订单可含餐饮、礼盒、出行等多个部分
    # ------------------------------------------------------------------

    def register_order(
        self,
        *,
        order_id: str,
        consumer_id: str,
        merchant_id: str,
        parts: Iterable[Mapping[str, Any]],
        placed_at: datetime,
        now: datetime,
        contact: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        require_aware(placed_at, "placed_at")
        require_aware(now, "now")
        if order_id in self._state["orders"]:
            raise StateError(f"订单 {order_id} 已登记")
        part_list = []
        for part in parts:
            category = str(part.get("category", ""))
            if category not in _VALID_CATEGORIES:
                raise DisputeError(f"订单部分使用了未登记的绿色品类: {category!r}")
            part_list.append(
                {
                    "part_id": str(part["part_id"]),
                    "category": category,
                    "attributes": copy.deepcopy(dict(part.get("attributes", {}))),
                    "status": "active",
                    "changes": [],
                }
            )
        order = {
            "order_id": order_id,
            "consumer_id": consumer_id,
            "merchant_id": merchant_id,
            "placed_at": iso(placed_at),
            "contact": dict(contact or {}),
            "parts": part_list,
        }
        self._state["orders"][order_id] = order
        self._state["seq"] += 1
        self._audit(consumer_id, "order_registered", now, detail={"order_id": order_id})
        self._persist()
        return copy.deepcopy(order)

    def record_order_change(
        self,
        order_id: str,
        part_id: str,
        *,
        change: str,
        actor: str,
        now: datetime,
    ) -> None:
        """登记退款或改签。只追加变更历史，不改写订单部分与商家承诺原文。"""
        require_aware(now, "now")
        if change not in _VALID_CHANGES:
            raise DisputeError(f"不支持的订单变更: {change!r}")
        order = self._get("orders", order_id)
        for part in order["parts"]:
            if part["part_id"] == part_id:
                part["status"] = change
                part["changes"].append({"change": change, "actor": actor, "at": iso(now)})
                self._state["seq"] += 1
                self._audit(
                    actor,
                    "order_part_changed",
                    now,
                    detail={"order_id": order_id, "part_id": part_id, "change": change},
                )
                self._persist()
                return
        raise NotFoundError(f"订单 {order_id} 没有部分 {part_id}")

    # ------------------------------------------------------------------
    # 立案：锁定规则版本与承诺版本，生成期限
    # ------------------------------------------------------------------

    def open_case(
        self,
        *,
        order_id: str,
        claim_ids: Iterable[str],
        consumer_id: str,
        now: datetime,
    ) -> dict[str, Any]:
        """消费者就一笔订单立案。立案时锁定当时生效的规则版本与各承诺的最新版本。"""
        require_aware(now, "now")
        order = self._get("orders", order_id)
        if order["consumer_id"] != consumer_id:
            raise AuthorizationError("只能就本人订单立案")
        claim_refs = []
        for claim_id in claim_ids:
            claim = self.get_claim(claim_id)
            claim_refs.append(
                {
                    "claim_id": claim["claim_id"],
                    "version": claim["version"],
                    "merchant_id": claim["merchant_id"],
                }
            )
        ruleset = self._ruleset_at(now)
        case_id = self._next_id("case")
        case = {
            "case_id": case_id,
            "order_id": order_id,
            "consumer_id": consumer_id,
            "merchant_id": order["merchant_id"],
            "claim_refs": claim_refs,
            "rule_version": ruleset["version"],
            "status": CaseStatus.OPEN.value,
            "opened_at": iso(now),
            "response_deadline": iso(now + self._response_window),
            "decide_deadline": iso(now + self._decide_window),
            "evidence_ids": [],
            "conflicts": [],
            "frozen_reason": None,
            "decision": None,
            "remedy": None,
        }
        self._state["cases"][case_id] = case
        self._audit(
            consumer_id,
            "case_opened",
            now,
            case_id=case_id,
            detail={"order_id": order_id, "rule_version": ruleset["version"]},
        )
        self._persist()
        return copy.deepcopy(case)

    # ------------------------------------------------------------------
    # 凭证：幂等受理、冲突冻结、分级与防篡改
    # ------------------------------------------------------------------

    def submit_evidence(
        self,
        *,
        case_id: str,
        evidence_id: str,
        uploader_role: str,
        uploader_id: str,
        kind: str,
        content: Any,
        now: datetime,
        authorized: bool = False,
    ) -> dict[str, Any]:
        """受理凭证。

        - 同一 evidence_id 且内容一致：返回既有受理回执，不重复立案；
        - 同一 evidence_id 但内容冲突：冻结争议并抛 EvidenceConflictError，不任选一份；
        - 商家上传与消费者原始凭证同号但内容不同的材料：拒绝并抛 TamperError；
        - 订单类凭证必须持有消费者授权。
        """
        require_aware(now, "now")
        if uploader_role not in UPLOADER_CLASSIFICATION:
            raise DisputeError(f"未登记的上传方角色: {uploader_role!r}")
        if kind == ORDER_EVIDENCE_KIND and not authorized:
            raise AuthorizationError("订单类凭证必须经消费者授权")
        case = self._get_case(case_id)
        digest = content_digest(content)
        existing = self._state["evidence"].get(evidence_id)
        if existing is not None:
            if existing["content_hash"] == digest:
                return copy.deepcopy(self._state["receipts"][existing["receipt_id"]])
            if (
                existing["classification"] == EvidenceClass.FACT_MATERIAL.value
                and uploader_role == "merchant"
            ):
                self._state["seq"] += 1
                self._audit(
                    uploader_id,
                    "evidence_tamper_rejected",
                    now,
                    case_id=case_id,
                    detail={"evidence_id": evidence_id},
                )
                self._persist()
                raise TamperError("商家不能改写消费者原始凭证")
            conflict = {
                "evidence_id": evidence_id,
                "existing_receipt_id": existing["receipt_id"],
                "attempted_by": uploader_id,
                "attempted_role": uploader_role,
                "at": iso(now),
            }
            case["conflicts"].append(conflict)
            case["status"] = CaseStatus.FROZEN.value
            case["frozen_reason"] = f"凭证 {evidence_id} 内容冲突"
            self._state["seq"] += 1
            self._audit(
                uploader_id,
                "evidence_conflict_frozen",
                now,
                case_id=case_id,
                detail={"evidence_id": evidence_id},
            )
            self._persist()
            raise EvidenceConflictError(f"凭证 {evidence_id} 内容冲突，争议已冻结")
        if case["status"] != CaseStatus.OPEN.value:
            raise StateError(f"案件状态为 {case['status']}，不能接收新凭证")
        classification = UPLOADER_CLASSIFICATION[uploader_role]
        receipt_id = self._next_id("rcpt")
        receipt = {
            "receipt_id": receipt_id,
            "evidence_id": evidence_id,
            "case_id": case_id,
            "classification": classification.value,
            "kind": kind,
            "accepted_at": iso(now),
        }
        self._state["receipts"][receipt_id] = receipt
        self._state["evidence"][evidence_id] = {
            "evidence_id": evidence_id,
            "case_id": case_id,
            "uploader_role": uploader_role,
            "uploader_id": uploader_id,
            "kind": kind,
            "content_hash": digest,
            "classification": classification.value,
            "submitted_at": iso(now),
            "receipt_id": receipt_id,
        }
        case["evidence_ids"].append(evidence_id)
        self._audit(
            uploader_id,
            "evidence_received",
            now,
            case_id=case_id,
            detail={"evidence_id": evidence_id, "classification": classification.value},
        )
        self._persist()
        return copy.deepcopy(receipt)

    def unfreeze_case(self, case_id: str, *, actor: str, note: str, now: datetime) -> None:
        """调解员人工核对冲突后解冻；冲突记录保留，不删除任何一份凭证。"""
        require_aware(now, "now")
        if not note.strip():
            raise DisputeError("解冻必须说明核对结论")
        case = self._get_case(case_id)
        if case["status"] != CaseStatus.FROZEN.value:
            raise StateError("只有冻结中的案件可以解冻")
        case["status"] = CaseStatus.OPEN.value
        case["frozen_reason"] = None
        self._state["seq"] += 1
        self._audit(actor, "case_unfrozen", now, case_id=case_id, detail={"note": note})
        self._persist()

    # ------------------------------------------------------------------
    # 评估：只有符合对应规则的部分计入绿色行动；退款不抹掉原承诺
    # ------------------------------------------------------------------

    @staticmethod
    def _matches(criteria: Mapping[str, Any], attributes: Mapping[str, Any]) -> bool:
        for key, expected in criteria.items():
            actual = attributes.get(key)
            if isinstance(expected, (list, tuple, set)):
                if actual not in expected:
                    return False
            elif actual != expected:
                return False
        return True

    def evaluate_case(self, case_id: str) -> dict[str, Any]:
        """按案件锁定的规则版本逐部分评估订单。

        promised_at_open 表示该部分按承诺本应计入绿色行动；counts_now 表示
        当前仍计入（未退款、未改签）。退款/改签只影响 counts_now，不影响
        承诺与规则版本的事实记录。
        """
        case = self._get_case(case_id)
        ruleset = self._ruleset_by_version(case["rule_version"])
        order = self._get("orders", case["order_id"])
        parts = []
        for part in order["parts"]:
            criteria = ruleset["criteria"].get(part["category"])
            matched = criteria is not None and self._matches(criteria, part["attributes"])
            parts.append(
                {
                    "part_id": part["part_id"],
                    "category": part["category"],
                    "status": part["status"],
                    "matched_rule": matched,
                    "promised_at_open": matched,
                    "counts_now": matched and part["status"] == "active",
                }
            )
        return {
            "case_id": case_id,
            "rule_version": ruleset["version"],
            "parts": parts,
        }

    # ------------------------------------------------------------------
    # 调解结论与补救：可执行、可回溯
    # ------------------------------------------------------------------

    def decide_case(
        self,
        case_id: str,
        *,
        actor: str,
        outcomes: Iterable[Mapping[str, Any]],
        now: datetime,
    ) -> dict[str, Any]:
        """形成调解结论。每项主张记录所用承诺版本与规则版本。"""
        require_aware(now, "now")
        case = self._get_case(case_id)
        if case["status"] != CaseStatus.OPEN.value:
            raise StateError(f"案件状态为 {case['status']}，不能出具调解结论")
        known = {ref["claim_id"]: ref for ref in case["claim_refs"]}
        decided = []
        for outcome in outcomes:
            claim_id = str(outcome["claim_id"])
            if claim_id not in known:
                raise NotFoundError(f"案件 {case_id} 未引用承诺 {claim_id}")
            decided.append(
                {
                    "claim_id": claim_id,
                    "claim_version": known[claim_id]["version"],
                    "rule_version": case["rule_version"],
                    "upheld": bool(outcome["upheld"]),
                    "rationale": str(outcome.get("rationale", "")),
                }
            )
        if not decided:
            raise DisputeError("调解结论至少要覆盖一项主张")
        case["decision"] = {"decided_by": actor, "decided_at": iso(now), "outcomes": decided}
        case["status"] = CaseStatus.DECIDED.value
        self._state["seq"] += 1
        self._audit(actor, "mediation_decided", now, case_id=case_id, detail={"outcomes": len(decided)})
        self._persist()
        return copy.deepcopy(case["decision"])

    def confirm_remedy(
        self,
        case_id: str,
        *,
        actor: str,
        kind: str,
        detail: str,
        now: datetime,
    ) -> dict[str, Any]:
        """确认可执行的补偿或整改，并生成待外发通知。"""
        require_aware(now, "now")
        if kind not in {item.value for item in RemedyKind}:
            raise DisputeError(f"不支持的补救类型: {kind!r}")
        case = self._get_case(case_id)
        if case["status"] != CaseStatus.DECIDED.value:
            raise StateError("只有已出具调解结论的案件才能确认补救")
        remedy = {
            "kind": kind,
            "detail": detail,
            "enforceable": True,
            "confirmed_by": actor,
            "confirmed_at": iso(now),
        }
        case["remedy"] = remedy
        case["status"] = CaseStatus.REMEDY_CONFIRMED.value
        notification_id = self._next_id("ntf")
        self._state["outbox"].append(
            {
                "notification_id": notification_id,
                "case_id": case_id,
                "kind": kind,
                "detail": detail,
                "queued_at": iso(now),
                "delivered_at": None,
            }
        )
        self._audit(actor, "remedy_confirmed", now, case_id=case_id, detail={"kind": kind})
        self._persist()
        return copy.deepcopy(remedy)

    # ------------------------------------------------------------------
    # 期限与外发：重启后继续，渠道不可用时滞留待发
    # ------------------------------------------------------------------

    def overdue_cases(self, now: datetime) -> list[dict[str, Any]]:
        """按已保存的期限计算超期案件；期限随状态落盘，重启后继续计时。"""
        require_aware(now, "now")
        overdue = []
        for case in self._state["cases"].values():
            if case["status"] in TERMINAL_STATUSES:
                continue
            decide_deadline = parse_time(case["decide_deadline"])
            response_deadline = parse_time(case["response_deadline"])
            if now > decide_deadline or now > response_deadline:
                overdue.append(
                    {
                        "case_id": case["case_id"],
                        "status": case["status"],
                        "response_deadline": case["response_deadline"],
                        "decide_deadline": case["decide_deadline"],
                        "response_overdue": now > response_deadline,
                        "decide_overdue": now > decide_deadline,
                    }
                )
        return sorted(overdue, key=lambda item: item["case_id"])

    def flush_outbox(self, deliver: Callable[[Mapping[str, Any]], None], now: datetime) -> list[str]:
        """向外部投诉渠道补发通知。渠道不可用（deliver 抛错）时通知滞留待发。"""
        require_aware(now, "now")
        delivered = []
        changed = False
        for notification in self._state["outbox"]:
            if notification["delivered_at"] is not None:
                continue
            try:
                deliver(copy.deepcopy(notification))
            except Exception:
                continue
            notification["delivered_at"] = iso(now)
            delivered.append(notification["notification_id"])
            changed = True
        if changed:
            self._persist()
        return delivered

    def pending_notifications(self) -> list[dict[str, Any]]:
        return [
            copy.deepcopy(item) for item in self._state["outbox"] if item["delivered_at"] is None
        ]

    # ------------------------------------------------------------------
    # 视图：调解员最小化视图与案件全量回溯
    # ------------------------------------------------------------------

    def mediator_view(self, case_id: str) -> dict[str, Any]:
        """调解员视图：只提供处理争议所必需的个人信息。"""
        case = self._get_case(case_id)
        order = self._get("orders", case["order_id"])
        evidence = [
            {
                "evidence_id": item["evidence_id"],
                "classification": item["classification"],
                "kind": item["kind"],
                "submitted_at": item["submitted_at"],
                "receipt_id": item["receipt_id"],
            }
            for item in (self._state["evidence"][eid] for eid in case["evidence_ids"])
        ]
        return {
            "case_id": case["case_id"],
            "status": case["status"],
            "consumer_ref": pseudonym(case["consumer_id"]),
            "consumer_contact": mask_contact(order["contact"]),
            "merchant_id": case["merchant_id"],
            "order_parts": [
                {
                    "part_id": part["part_id"],
                    "category": part["category"],
                    "attributes": copy.deepcopy(part["attributes"]),
                    "status": part["status"],
                }
                for part in order["parts"]
            ],
            "claim_refs": copy.deepcopy(case["claim_refs"]),
            "rule_version": case["rule_version"],
            "evidence": evidence,
            "conflicts": copy.deepcopy(case["conflicts"]),
            "response_deadline": case["response_deadline"],
            "decide_deadline": case["decide_deadline"],
            "decision": copy.deepcopy(case["decision"]),
            "remedy": copy.deepcopy(case["remedy"]),
        }

    def case_report(self, case_id: str) -> dict[str, Any]:
        """案件说明：每项主张采用的规则版本、经手人轨迹、补救是否可执行。"""
        case = self._get_case(case_id)
        claims = []
        outcomes = (case["decision"] or {}).get("outcomes", [])
        by_claim = {item["claim_id"]: item for item in outcomes}
        for ref in case["claim_refs"]:
            outcome = by_claim.get(ref["claim_id"])
            claims.append(
                {
                    "claim_id": ref["claim_id"],
                    "claim_version": ref["version"],
                    "rule_version": case["rule_version"],
                    "upheld": outcome["upheld"] if outcome else None,
                    "rationale": outcome["rationale"] if outcome else None,
                }
            )
        handlers = [
            {"actor": entry["actor"], "action": entry["action"], "at": entry["at"]}
            for entry in self._state["audit"]
            if entry["case_id"] == case_id
        ]
        remedy = copy.deepcopy(case["remedy"])
        return {
            "case_id": case_id,
            "status": case["status"],
            "claims": claims,
            "handlers": handlers,
            "remedy": remedy,
            "remedy_enforceable": bool(remedy and remedy["enforceable"]),
        }
