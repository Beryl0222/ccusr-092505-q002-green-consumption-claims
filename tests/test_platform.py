from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from green_consumption_claims.domain import (
    SCOPE_OPEN_CASE,
    SCOPE_ORDER_EVIDENCE,
    ActorRole,
    Authorization,
    CaseStatus,
    ConsumerOrder,
    DisputeItem,
    EvidenceKind,
    GreenRule,
    IntakeOutcome,
    LineCategory,
    MaterialClass,
    OrderChangeType,
    OrderLine,
    PromisedAction,
    RemedyType,
)
from green_consumption_claims.platform import (
    ChannelUnavailable,
    DisputePlatform,
    InvalidState,
    PermissionDenied,
)
from green_consumption_claims.store import InMemoryEventStore, JsonlEventStore

SCHEMA = json.loads((ROOT / "contracts" / "domain.schema.json").read_text(encoding="utf-8"))
TZ = timezone(timedelta(hours=8))
T0 = datetime(2026, 9, 20, 12, 0, tzinfo=TZ)
DAY = timedelta(days=1)

RULES_V1 = [
    GreenRule(LineCategory.DINING, "小份餐", "餐饮小份餐可计入绿色行动"),
    GreenRule(LineCategory.GIFT_BOX, "纸袋包装", "礼盒纸袋包装可计入绿色行动"),
]
PROMISED_V1 = [
    PromisedAction(LineCategory.DINING, "小份餐", "提供小份餐选项"),
    PromisedAction(LineCategory.GIFT_BOX, "纸袋包装", "礼盒使用纸袋包装"),
    PromisedAction(LineCategory.GIFT_BOX, "可降解填充", "礼盒使用可降解填充物"),
]


def seed_rules_and_claim(platform: DisputePlatform) -> None:
    platform.publish_rules(version=1, rules=RULES_V1, issued_at=T0, at=T0)
    platform.publish_claim(
        merchant_id="m-1",
        claim_id="claim-1",
        version=1,
        window_start=T0,
        window_end=T0 + 30 * DAY,
        applicable_products=frozenset({"p-meal", "p-box"}),
        promised_actions=PROMISED_V1,
        summary="双节绿色承诺v1",
        at=T0,
    )


def register_sample_order(
    platform: DisputePlatform, order_id: str = "o-1", consumer_id: str = "c-1", placed_at: datetime | None = None
) -> ConsumerOrder:
    order = ConsumerOrder(
        order_id=order_id,
        consumer_id=consumer_id,
        placed_at=placed_at or T0 + DAY,
        lines=(
            OrderLine("l-meal", LineCategory.DINING, "p-meal", "双人餐", ("小份餐",)),
            OrderLine("l-box", LineCategory.GIFT_BOX, "p-box", "中秋礼盒", ("纸袋包装", "可降解填充")),
            OrderLine("l-trip", LineCategory.TRAVEL, "p-trip", "景区直通车", ("绿色充电",)),
        ),
    )
    platform.register_order(
        order=order,
        authorization=Authorization(consumer_id, frozenset({SCOPE_ORDER_EVIDENCE, SCOPE_OPEN_CASE}), T0),
        at=order.placed_at,
    )
    return order


def open_sample_case(platform: DisputePlatform, case_id: str = "case-1", order_id: str = "o-1", consumer_id: str = "c-1"):
    return platform.open_case(
        case_id=case_id,
        order_id=order_id,
        claim_id="claim-1",
        items=[
            DisputeItem("i-1", "l-meal", "小份餐", "商家承诺小份餐，实际未提供"),
            DisputeItem("i-2", "l-box", "纸袋包装", "礼盒未使用承诺的纸袋包装"),
        ],
        opened_by="intake-1",
        authorization=Authorization(consumer_id, frozenset({SCOPE_OPEN_CASE, SCOPE_ORDER_EVIDENCE}), T0),
        at=T0 + 2 * DAY,
    )


def make_platform() -> DisputePlatform:
    platform = DisputePlatform(InMemoryEventStore(), schema=SCHEMA)
    seed_rules_and_claim(platform)
    register_sample_order(platform)
    open_sample_case(platform)
    return platform


def submit_photo(platform: DisputePlatform, evidence_id: str, content: str, case_id: str = "case-1", **overrides):
    params = dict(
        case_id=case_id,
        evidence_id=evidence_id,
        kind=EvidenceKind.PACKAGING_PHOTO,
        material_class=MaterialClass.FACTUAL,
        submitted_by=ActorRole.CONSUMER,
        actor_id="c-1",
        content=content,
        payload={"note": "包装照片"},
        consumer_original=True,
        at=T0 + 3 * DAY,
    )
    params.update(overrides)
    return platform.submit_evidence(**params)


class GreenCreditTests(unittest.TestCase):
    def test_only_matching_parts_of_mixed_order_count(self) -> None:
        platform = make_platform()
        credits = platform.green_credits("case-1")
        by_line: dict[str, set[str]] = {}
        for credit in credits:
            by_line.setdefault(credit.line_id, set()).add(credit.action)
            self.assertEqual(1, credit.rule_version)
            self.assertEqual(1, credit.claim_version)
        # 餐饮小份餐与礼盒纸袋包装符合承诺与规则；出行绿色充电未被承诺，
        # 礼盒可降解填充未被规则 v1 认可，均不计入。
        self.assertEqual({"小份餐"}, by_line.get("l-meal"))
        self.assertEqual({"纸袋包装"}, by_line.get("l-box"))
        self.assertNotIn("l-trip", by_line)

    def test_refund_and_rebook_do_not_erase_commitment(self) -> None:
        platform = make_platform()
        before = platform.green_credits("case-1")
        platform.record_order_change(
            order_id="o-1", change_type=OrderChangeType.REFUND, line_id="l-meal", note="消费者退款", at=T0 + 4 * DAY
        )
        platform.record_order_change(
            order_id="o-1", change_type=OrderChangeType.REBOOK, line_id="l-trip", note="改签出行日期", at=T0 + 4 * DAY
        )
        self.assertEqual(before, platform.green_credits("case-1"))
        self.assertEqual(2, len(platform.order_changes("o-1")))
        explanation = platform.explain_case("case-1")
        self.assertEqual("双节绿色承诺v1", explanation.claim_summary)
        self.assertTrue(all(item.claim_version == 1 for item in explanation.items))


class EvidenceIntakeTests(unittest.TestCase):
    def test_duplicate_submission_returns_existing_acceptance(self) -> None:
        platform = make_platform()
        first = submit_photo(platform, "ev-1", "照片内容甲")
        self.assertEqual(IntakeOutcome.ACCEPTED, first.outcome)
        second = submit_photo(platform, "ev-1", "照片内容甲")
        self.assertEqual(IntakeOutcome.DUPLICATE, second.outcome)
        self.assertEqual(first.acceptance_id, second.acceptance_id)
        self.assertEqual(first.accepted_at, second.accepted_at)
        self.assertEqual(1, len(platform.mediator_view("case-1")["evidence"]))

    def test_conflicting_content_freezes_case_and_keeps_original(self) -> None:
        platform = make_platform()
        submit_photo(platform, "ev-1", "照片内容甲")
        result = submit_photo(platform, "ev-1", "照片内容乙")
        self.assertEqual(IntakeOutcome.CONFLICT_FROZEN, result.outcome)
        self.assertEqual(CaseStatus.FROZEN, platform.case_status("case-1"))
        original = platform.get_evidence("ev-1")
        self.assertIsNotNone(original)
        self.assertEqual(hashlib.sha256("照片内容甲".encode("utf-8")).hexdigest(), original.content_hash)
        with self.assertRaises(InvalidState):
            platform.decide_item(
                case_id="case-1", item_id="i-1", upheld=True, mediator_id="med-1", rationale="冻结中", at=T0 + 4 * DAY
            )

    def test_merchant_cannot_modify_consumer_original_evidence(self) -> None:
        platform = make_platform()
        submit_photo(platform, "ev-9", "原始回收凭条")
        with self.assertRaises(PermissionDenied):
            submit_photo(
                platform,
                "ev-9",
                "篡改后的凭条",
                submitted_by=ActorRole.MERCHANT,
                actor_id="m-1",
                consumer_original=False,
                material_class=MaterialClass.MERCHANT_SELF,
            )
        self.assertEqual(CaseStatus.OPEN, platform.case_status("case-1"))

    def test_order_evidence_requires_consumer_authorization(self) -> None:
        platform = make_platform()
        with self.assertRaises(PermissionDenied):
            platform.submit_evidence(
                case_id="case-1",
                evidence_id="ev-order",
                kind=EvidenceKind.ORDER_RECORD,
                material_class=MaterialClass.FACTUAL,
                submitted_by=ActorRole.CONSUMER,
                actor_id="c-1",
                content="订单快照",
                payload={},
                at=T0 + 3 * DAY,
            )
        with self.assertRaises(PermissionDenied):
            platform.submit_evidence(
                case_id="case-1",
                evidence_id="ev-order",
                kind=EvidenceKind.ORDER_RECORD,
                material_class=MaterialClass.FACTUAL,
                submitted_by=ActorRole.CONSUMER,
                actor_id="c-1",
                content="订单快照",
                payload={},
                authorization=Authorization("someone-else", frozenset({SCOPE_ORDER_EVIDENCE}), T0),
                at=T0 + 3 * DAY,
            )
        accepted = platform.submit_evidence(
            case_id="case-1",
            evidence_id="ev-order",
            kind=EvidenceKind.ORDER_RECORD,
            material_class=MaterialClass.FACTUAL,
            submitted_by=ActorRole.CONSUMER,
            actor_id="c-1",
            content="订单快照",
            payload={},
            authorization=Authorization("c-1", frozenset({SCOPE_ORDER_EVIDENCE}), T0),
            at=T0 + 3 * DAY,
        )
        self.assertEqual(IntakeOutcome.ACCEPTED, accepted.outcome)

    def test_mediation_conclusion_only_by_mediator(self) -> None:
        platform = make_platform()
        with self.assertRaises(PermissionDenied):
            submit_photo(platform, "ev-c", "结论", material_class=MaterialClass.MEDIATION_CONCLUSION)
        result = submit_photo(
            platform,
            "ev-c",
            "结论",
            material_class=MaterialClass.MEDIATION_CONCLUSION,
            submitted_by=ActorRole.MEDIATOR,
            actor_id="med-1",
            consumer_original=False,
        )
        self.assertEqual(IntakeOutcome.ACCEPTED, result.outcome)


class PermissionAndViewTests(unittest.TestCase):
    def test_mediator_view_masks_personal_information(self) -> None:
        platform = make_platform()
        platform.submit_evidence(
            case_id="case-1",
            evidence_id="ev-pii",
            kind=EvidenceKind.RECYCLING_HANDOVER,
            material_class=MaterialClass.FACTUAL,
            submitted_by=ActorRole.CONSUMER,
            actor_id="c-1",
            content="回收交接单",
            payload={
                "consumer_name": "张小明",
                "phone": "13812345678",
                "address": "上海市浦东新区某某路100号",
                "note": "旧物回收交接",
            },
            consumer_original=True,
            at=T0 + 3 * DAY,
        )
        view = platform.mediator_view("case-1")
        payload = view["evidence"][0]["payload"]
        self.assertEqual("张**", payload["consumer_name"])
        self.assertEqual("138****78", payload["phone"])
        self.assertEqual("上海市浦东新***", payload["address"])
        self.assertEqual("旧物回收交接", payload["note"])
        # 平台内部仍保留原始凭证内容
        stored = platform.get_evidence("ev-pii")
        self.assertEqual("13812345678", stored.payload["phone"])

    def test_material_classes_are_distinguished_in_view(self) -> None:
        platform = make_platform()
        submit_photo(platform, "ev-fact", "照片")
        submit_photo(
            platform,
            "ev-self",
            "商家说明",
            submitted_by=ActorRole.MERCHANT,
            actor_id="m-1",
            consumer_original=False,
            material_class=MaterialClass.MERCHANT_SELF,
        )
        classes = {item["evidence_id"]: item["material_class"] for item in platform.mediator_view("case-1")["evidence"]}
        self.assertEqual("事实材料", classes["ev-fact"])
        self.assertEqual("商家自证", classes["ev-self"])


class RuleAndClaimVersionTests(unittest.TestCase):
    def test_rule_update_applies_only_to_new_cases(self) -> None:
        platform = make_platform()
        case1_actions = {credit.action for credit in platform.green_credits("case-1") if credit.line_id == "l-box"}
        self.assertEqual({"纸袋包装"}, case1_actions)
        platform.publish_rules(
            version=2,
            rules=RULES_V1 + [GreenRule(LineCategory.GIFT_BOX, "可降解填充", "可降解填充物可计入绿色行动")],
            issued_at=T0 + 5 * DAY,
            at=T0 + 5 * DAY,
        )
        register_sample_order(platform, order_id="o-2", placed_at=T0 + 6 * DAY)
        open_sample_case(platform, case_id="case-2", order_id="o-2")
        self.assertEqual(1, platform.mediator_view("case-1")["rule_version"])
        self.assertEqual(2, platform.mediator_view("case-2")["rule_version"])
        case1_after = {credit.action for credit in platform.green_credits("case-1") if credit.line_id == "l-box"}
        case2_actions = {credit.action for credit in platform.green_credits("case-2") if credit.line_id == "l-box"}
        self.assertEqual({"纸袋包装"}, case1_after)
        self.assertEqual({"纸袋包装", "可降解填充"}, case2_actions)

    def test_claim_version_selected_by_order_time(self) -> None:
        platform = make_platform()
        platform.publish_claim(
            merchant_id="m-1",
            claim_id="claim-1",
            version=2,
            window_start=T0 + 5 * DAY,
            window_end=T0 + 40 * DAY,
            applicable_products=frozenset({"p-meal"}),
            promised_actions=[PromisedAction(LineCategory.DINING, "小份餐", "提供小份餐选项")],
            summary="双节绿色承诺v2",
            at=T0 + 5 * DAY,
        )
        register_sample_order(platform, order_id="o-late", placed_at=T0 + 6 * DAY)
        open_sample_case(platform, case_id="case-late", order_id="o-late")
        self.assertEqual(1, platform.mediator_view("case-1")["claim_version"])
        self.assertEqual(2, platform.mediator_view("case-late")["claim_version"])
        with self.assertRaises(InvalidState):
            platform.publish_claim(
                merchant_id="m-1",
                claim_id="claim-1",
                version=2,
                window_start=T0,
                window_end=T0 + 10 * DAY,
                applicable_products=frozenset({"p-meal"}),
                promised_actions=[],
                summary="重复版本",
                at=T0 + 7 * DAY,
            )

    def test_case_before_any_claim_version_is_rejected(self) -> None:
        platform = DisputePlatform(InMemoryEventStore(), schema=SCHEMA)
        seed_rules_and_claim(platform)
        register_sample_order(platform, order_id="o-early", placed_at=T0 - 2 * DAY)
        with self.assertRaises(InvalidState):
            open_sample_case(platform, case_id="case-early", order_id="o-early")


class PersistenceTests(unittest.TestCase):
    def test_restart_preserves_acceptances_and_deadlines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "events.jsonl"
            marker_path = Path(tmp) / "sync.marker"
            first = DisputePlatform(JsonlEventStore(store_path), schema=SCHEMA, sync_marker_path=marker_path)
            seed_rules_and_claim(first)
            register_sample_order(first)
            due = open_sample_case(first)
            accepted = submit_photo(first, "ev-1", "照片内容甲")

            restored = DisputePlatform(JsonlEventStore(store_path), schema=SCHEMA, sync_marker_path=marker_path)
            acceptance = restored.get_acceptance("ev-1")
            self.assertIsNotNone(acceptance)
            self.assertEqual(accepted.acceptance_id, acceptance.acceptance_id)
            self.assertEqual(accepted.accepted_at, acceptance.accepted_at)
            again = submit_photo(restored, "ev-1", "照片内容甲")
            self.assertEqual(IntakeOutcome.DUPLICATE, again.outcome)
            self.assertEqual(accepted.acceptance_id, again.acceptance_id)
            self.assertEqual(due, restored.case_deadline("case-1"))
            self.assertEqual(("case-1", due), restored.overdue_cases(due + timedelta(seconds=1))[0])
            # 重启后事件序号连续，可继续受理
            restored.publish_rules(version=2, rules=RULES_V1, issued_at=T0 + 6 * DAY, at=T0 + 6 * DAY)
            register_sample_order(restored, order_id="o-2", placed_at=T0 + 6 * DAY)
            open_sample_case(restored, case_id="case-2", order_id="o-2")
            self.assertEqual(2, restored.mediator_view("case-2")["rule_version"])

    def test_external_channel_outage_keeps_local_state(self) -> None:
        class FakeChannel:
            def __init__(self) -> None:
                self.available = False
                self.received: list[dict] = []

            def push(self, event) -> None:
                if not self.available:
                    raise ChannelUnavailable("外部投诉渠道暂不可用")
                self.received.append(dict(event))

        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "events.jsonl"
            marker_path = Path(tmp) / "sync.marker"
            channel = FakeChannel()
            platform = DisputePlatform(JsonlEventStore(store_path), schema=SCHEMA, sync_marker_path=marker_path)
            seed_rules_and_claim(platform)
            register_sample_order(platform)
            open_sample_case(platform)
            self.assertEqual(4, platform.pending_external_count())
            self.assertEqual(0, platform.sync_external(channel))
            # 渠道不可用期间，本地受理与期限照常
            accepted = submit_photo(platform, "ev-1", "照片内容甲")
            self.assertEqual(IntakeOutcome.ACCEPTED, accepted.outcome)
            self.assertEqual(5, platform.pending_external_count())
            channel.available = True
            self.assertEqual(5, platform.sync_external(channel))
            self.assertEqual(0, platform.pending_external_count())
            self.assertEqual(
                [f"EV-{index:06d}" for index in range(1, 6)], [event["event_id"] for event in channel.received]
            )
            # 重启后同步进度保留，不重复推送
            restored = DisputePlatform(JsonlEventStore(store_path), schema=SCHEMA, sync_marker_path=marker_path)
            self.assertEqual(0, restored.pending_external_count())
            self.assertEqual(0, restored.sync_external(channel))
            self.assertEqual(5, len(channel.received))


class ExplanationTests(unittest.TestCase):
    def test_explanation_reports_versions_handlers_and_remedy(self) -> None:
        platform = make_platform()
        submit_photo(platform, "ev-1", "照片内容甲")
        with self.assertRaises(InvalidState):
            platform.confirm_remedy(
                case_id="case-1",
                remedy_type=RemedyType.COMPENSATION,
                detail="尚有主张未结论",
                enforceable=True,
                confirmed_by="med-1",
                at=T0 + 4 * DAY,
            )
        platform.decide_item(
            case_id="case-1", item_id="i-1", upheld=True, mediator_id="med-1", rationale="承诺未兑现", at=T0 + 4 * DAY
        )
        platform.decide_item(
            case_id="case-1",
            item_id="i-2",
            upheld=False,
            mediator_id="med-2",
            rationale="照片显示已用纸袋",
            at=T0 + 5 * DAY,
        )
        platform.confirm_remedy(
            case_id="case-1",
            remedy_type=RemedyType.COMPENSATION,
            detail="退还差价并补偿绿色积分",
            enforceable=True,
            confirmed_by="med-1",
            at=T0 + 6 * DAY,
        )
        explanation = platform.explain_case("case-1")
        self.assertEqual(CaseStatus.REMEDY_CONFIRMED, explanation.status)
        items = {item.item_id: item for item in explanation.items}
        self.assertEqual(("intake-1", "med-1"), items["i-1"].handlers)
        self.assertEqual(("intake-1", "med-2"), items["i-2"].handlers)
        self.assertTrue(items["i-1"].upheld)
        self.assertFalse(items["i-2"].upheld)
        self.assertTrue(all(item.rule_version == 1 and item.claim_version == 1 for item in explanation.items))
        self.assertIsNotNone(explanation.remedy)
        self.assertEqual(RemedyType.COMPENSATION, explanation.remedy.remedy_type)
        self.assertTrue(explanation.enforceable_remedy)
        trail_actions = [entry["action"] for entry in platform.mediator_view("case-1")["trail"]]
        self.assertEqual(["立案受理", "登记调解决定：i-1", "登记调解决定：i-2", "确认补偿或整改"], trail_actions)


if __name__ == "__main__":
    unittest.main()
