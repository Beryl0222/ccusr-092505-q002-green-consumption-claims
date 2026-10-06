from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from green_consumption_claims import (  # noqa: E402
    AuthorizationError,
    DisputeDesk,
    EvidenceConflictError,
    StateError,
    TamperError,
)

TZ = timezone(timedelta(hours=8))
T0 = datetime(2026, 9, 24, 12, 0, tzinfo=TZ)


def at(**kwargs) -> datetime:
    return T0 + timedelta(**kwargs)


class DeskFixture(unittest.TestCase):
    """预置：一版规则、两版承诺、一笔含餐饮/礼盒/出行的订单、一个案件。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Path(self._tmp.name) / "desk.json"
        self.desk = DisputeDesk(self.store)
        self.desk.register_rules(
            version=1,
            effective_from=datetime(2026, 9, 1, tzinfo=TZ),
            criteria={
                "dining": {"small_portion": True},
                "gift_box": {"packaging": "paper_bag"},
                "travel": {"green_charging": True},
                "recycling": {"old_item_recycled": True},
            },
            now=T0,
        )
        self.desk.publish_claim(
            claim_id="claim-1",
            merchant_id="merchant-1",
            version=1,
            content="双节期间小份餐半价，纸袋包装，绿色充电，旧物回收",
            categories=["dining", "gift_box", "travel", "recycling"],
            valid_from=datetime(2026, 9, 20, tzinfo=TZ),
            valid_to=datetime(2026, 10, 10, tzinfo=TZ),
            now=T0,
        )
        self.desk.register_order(
            order_id="order-1",
            consumer_id="consumer-1",
            merchant_id="merchant-1",
            placed_at=datetime(2026, 9, 30, tzinfo=TZ),
            now=T0,
            contact={"name": "王小明", "phone": "13800001234"},
            parts=[
                {"part_id": "p-dining", "category": "dining", "attributes": {"small_portion": True}},
                {"part_id": "p-gift", "category": "gift_box", "attributes": {"packaging": "plastic"}},
                {"part_id": "p-travel", "category": "travel", "attributes": {"green_charging": True}},
            ],
        )
        self.case = self.desk.open_case(
            order_id="order-1", claim_ids=["claim-1"], consumer_id="consumer-1", now=at(days=1)
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def reopen(self) -> DisputeDesk:
        """模拟平台重启：从同一状态文件恢复。"""
        return DisputeDesk(self.store)


class ClaimTests(DeskFixture):
    def test_claim_versions_are_kept_and_monotonic(self) -> None:
        self.desk.publish_claim(
            claim_id="claim-1",
            merchant_id="merchant-1",
            version=2,
            content="活动口径调整后的承诺",
            categories=["dining"],
            valid_from=datetime(2026, 10, 1, tzinfo=TZ),
            valid_to=datetime(2026, 10, 10, tzinfo=TZ),
            now=at(days=2),
        )
        self.assertEqual("双节期间小份餐半价，纸袋包装，绿色充电，旧物回收",
                         self.desk.get_claim("claim-1", version=1)["content"])
        self.assertEqual(2, self.desk.get_claim("claim-1")["version"])
        with self.assertRaises(StateError):
            self.desk.publish_claim(
                claim_id="claim-1", merchant_id="merchant-1", version=2,
                content="改写历史版本", categories=["dining"],
                valid_from=datetime(2026, 10, 1, tzinfo=TZ),
                valid_to=datetime(2026, 10, 10, tzinfo=TZ), now=at(days=2),
            )

    def test_delist_keeps_promise_on_record(self) -> None:
        self.desk.delist_claim("claim-1", merchant_id="merchant-1", now=at(days=3))
        claim = self.desk.get_claim("claim-1", version=1)
        self.assertIsNotNone(claim["delisted_at"])
        self.assertIn("小份餐", claim["content"])

    def test_case_pins_claim_version_at_open(self) -> None:
        self.desk.publish_claim(
            claim_id="claim-1", merchant_id="merchant-1", version=2,
            content="新口径", categories=["dining"],
            valid_from=datetime(2026, 10, 1, tzinfo=TZ),
            valid_to=datetime(2026, 10, 10, tzinfo=TZ), now=at(days=2),
        )
        case = self.desk.open_case(
            order_id="order-1", claim_ids=["claim-1"], consumer_id="consumer-1", now=at(days=2)
        )
        self.assertEqual(2, case["claim_refs"][0]["version"])
        self.assertEqual(1, self.case["claim_refs"][0]["version"])


class RuleVersionTests(DeskFixture):
    def test_rule_update_applies_only_to_new_cases(self) -> None:
        self.desk.register_rules(
            version=2,
            effective_from=at(days=2),
            criteria={"gift_box": {"packaging": "plastic"}},
            now=at(days=2),
        )
        old_eval = self.desk.evaluate_case(self.case["case_id"])
        self.assertEqual(1, old_eval["rule_version"])
        new_case = self.desk.open_case(
            order_id="order-1", claim_ids=["claim-1"], consumer_id="consumer-1", now=at(days=3)
        )
        self.assertEqual(2, new_case["rule_version"])
        new_eval = self.desk.evaluate_case(new_case["case_id"])
        gift = next(p for p in new_eval["parts"] if p["part_id"] == "p-gift")
        self.assertTrue(gift["matched_rule"])


class EvaluationTests(DeskFixture):
    def test_only_matching_parts_count_as_green(self) -> None:
        result = self.desk.evaluate_case(self.case["case_id"])
        by_part = {p["part_id"]: p for p in result["parts"]}
        self.assertTrue(by_part["p-dining"]["counts_now"])
        self.assertFalse(by_part["p-gift"]["counts_now"])  # 塑料包装不符合纸袋规则
        self.assertTrue(by_part["p-travel"]["counts_now"])

    def test_refund_does_not_erase_original_promise(self) -> None:
        self.desk.record_order_change(
            "order-1", "p-dining", change="refunded", actor="consumer-1", now=at(days=2)
        )
        result = self.desk.evaluate_case(self.case["case_id"])
        dining = next(p for p in result["parts"] if p["part_id"] == "p-dining")
        self.assertTrue(dining["promised_at_open"])  # 原承诺仍在
        self.assertFalse(dining["counts_now"])       # 但不再计入当前绿色行动
        self.assertIn("小份餐", self.desk.get_claim("claim-1", version=1)["content"])


class EvidenceTests(DeskFixture):
    def submit_order_evidence(self, **overrides):
        params = dict(
            case_id=self.case["case_id"],
            evidence_id="ev-1",
            uploader_role="consumer",
            uploader_id="consumer-1",
            kind="order_record",
            content={"order_id": "order-1", "channel": "app"},
            now=at(days=1, hours=1),
            authorized=True,
        )
        params.update(overrides)
        return self.desk.submit_evidence(**params)

    def test_order_evidence_requires_consumer_authorization(self) -> None:
        with self.assertRaises(AuthorizationError):
            self.submit_order_evidence(authorized=False)

    def test_duplicate_upload_returns_existing_receipt(self) -> None:
        first = self.submit_order_evidence()
        again = self.submit_order_evidence(now=at(days=4))
        self.assertEqual(first, again)

    def test_conflicting_content_freezes_case(self) -> None:
        self.submit_order_evidence()
        with self.assertRaises(EvidenceConflictError):
            self.submit_order_evidence(
                evidence_id="ev-1",
                uploader_role="consumer",
                content={"order_id": "order-1", "channel": "被改动的内容"},
            )
        view = self.desk.mediator_view(self.case["case_id"])
        self.assertEqual("frozen", view["status"])
        self.assertEqual(1, len(view["conflicts"]))
        with self.assertRaises(StateError):
            self.desk.submit_evidence(
                case_id=self.case["case_id"], evidence_id="ev-2",
                uploader_role="consumer", uploader_id="consumer-1",
                kind="chat_log", content="冻结期间的新凭证",
                now=at(days=1, hours=2),
            )
        self.desk.unfreeze_case(
            self.case["case_id"], actor="mediator-1", note="已人工核对两份材料", now=at(days=2)
        )
        self.assertEqual("open", self.desk.mediator_view(self.case["case_id"])["status"])

    def test_merchant_cannot_overwrite_consumer_original(self) -> None:
        self.submit_order_evidence()
        with self.assertRaises(TamperError):
            self.desk.submit_evidence(
                case_id=self.case["case_id"], evidence_id="ev-1",
                uploader_role="merchant", uploader_id="merchant-1",
                kind="order_record", content={"order_id": "order-1", "channel": "商家口径"},
                now=at(days=1, hours=2), authorized=True,
            )
        self.assertEqual("open", self.desk.mediator_view(self.case["case_id"])["status"])

    def test_evidence_is_classified_by_uploader(self) -> None:
        self.submit_order_evidence()
        self.desk.submit_evidence(
            case_id=self.case["case_id"], evidence_id="ev-m1",
            uploader_role="merchant", uploader_id="merchant-1",
            kind="packaging_photo", content="商家自证包装照片",
            now=at(days=1, hours=2),
        )
        self.desk.submit_evidence(
            case_id=self.case["case_id"], evidence_id="ev-r1",
            uploader_role="consumer", uploader_id="consumer-1",
            kind="recycling_receipt", content="旧物回收交接凭条",
            now=at(days=1, hours=3),
        )
        view = self.desk.mediator_view(self.case["case_id"])
        classes = {item["evidence_id"]: item["classification"] for item in view["evidence"]}
        self.assertEqual("fact_material", classes["ev-1"])
        self.assertEqual("merchant_self_attestation", classes["ev-m1"])
        self.assertEqual("fact_material", classes["ev-r1"])


class PrivacyTests(DeskFixture):
    def test_mediator_view_minimizes_personal_info(self) -> None:
        view = self.desk.mediator_view(self.case["case_id"])
        self.assertNotIn("consumer-1", str(view))
        self.assertTrue(view["consumer_ref"].startswith("C-"))
        self.assertEqual("138****1234", view["consumer_contact"]["phone"])
        self.assertEqual("***", view["consumer_contact"]["name"])


class DecisionAndRemedyTests(DeskFixture):
    def test_decision_records_rule_and_claim_versions(self) -> None:
        decision = self.desk.decide_case(
            self.case["case_id"], actor="mediator-1",
            outcomes=[{"claim_id": "claim-1", "upheld": True, "rationale": "承诺与订单记录一致"}],
            now=at(days=3),
        )
        outcome = decision["outcomes"][0]
        self.assertEqual(1, outcome["claim_version"])
        self.assertEqual(1, outcome["rule_version"])

    def test_remedy_only_after_decision_and_is_enforceable(self) -> None:
        with self.assertRaises(StateError):
            self.desk.confirm_remedy(
                self.case["case_id"], actor="mediator-1",
                kind="compensation", detail="先行赔付", now=at(days=2),
            )
        self.desk.decide_case(
            self.case["case_id"], actor="mediator-1",
            outcomes=[{"claim_id": "claim-1", "upheld": True, "rationale": "成立"}],
            now=at(days=3),
        )
        remedy = self.desk.confirm_remedy(
            self.case["case_id"], actor="mediator-1",
            kind="rectification", detail="恢复活动页面承诺说明", now=at(days=4),
        )
        self.assertTrue(remedy["enforceable"])
        report = self.desk.case_report(self.case["case_id"])
        self.assertTrue(report["remedy_enforceable"])
        self.assertEqual("rectification", report["remedy"]["kind"])

    def test_report_explains_rules_handlers_and_remedy(self) -> None:
        self.desk.decide_case(
            self.case["case_id"], actor="mediator-1",
            outcomes=[{"claim_id": "claim-1", "upheld": False, "rationale": "证据不足"}],
            now=at(days=3),
        )
        self.desk.confirm_remedy(
            self.case["case_id"], actor="mediator-1",
            kind="compensation", detail="部分补偿", now=at(days=4),
        )
        report = self.desk.case_report(self.case["case_id"])
        self.assertEqual(1, report["claims"][0]["rule_version"])
        self.assertFalse(report["claims"][0]["upheld"])
        actors = [h["actor"] for h in report["handlers"]]
        self.assertIn("consumer-1", actors)   # 立案
        self.assertIn("mediator-1", actors)   # 调解与补救
        actions = [h["action"] for h in report["handlers"]]
        self.assertEqual(
            ["case_opened", "mediation_decided", "remedy_confirmed"], actions
        )


class PersistenceTests(DeskFixture):
    def test_restart_keeps_acceptance_and_deadlines(self) -> None:
        self.desk.submit_evidence(
            case_id=self.case["case_id"], evidence_id="ev-1",
            uploader_role="consumer", uploader_id="consumer-1",
            kind="chat_log", content="协商记录", now=at(days=1),
        )
        desk2 = self.reopen()
        again = desk2.submit_evidence(
            case_id=self.case["case_id"], evidence_id="ev-1",
            uploader_role="consumer", uploader_id="consumer-1",
            kind="chat_log", content="协商记录", now=at(days=2),
        )
        self.assertEqual(at(days=1).isoformat(), again["accepted_at"])
        overdue = desk2.overdue_cases(at(days=20))
        self.assertEqual([self.case["case_id"]], [item["case_id"] for item in overdue])
        self.assertTrue(overdue[0]["decide_overdue"])

    def test_outbox_survives_channel_outage_and_restart(self) -> None:
        self.desk.decide_case(
            self.case["case_id"], actor="mediator-1",
            outcomes=[{"claim_id": "claim-1", "upheld": True, "rationale": "成立"}],
            now=at(days=3),
        )
        self.desk.confirm_remedy(
            self.case["case_id"], actor="mediator-1",
            kind="compensation", detail="补偿 50 元", now=at(days=4),
        )

        def unavailable(_payload):
            raise ConnectionError("外部投诉渠道暂不可用")

        self.assertEqual([], self.desk.flush_outbox(unavailable, at(days=4)))
        desk2 = self.reopen()
        self.assertEqual(1, len(desk2.pending_notifications()))
        delivered_payloads = []
        delivered = desk2.flush_outbox(delivered_payloads.append, at(days=5))
        self.assertEqual(1, len(delivered))
        self.assertEqual("补偿 50 元", delivered_payloads[0]["detail"])
        self.assertEqual([], self.reopen().pending_notifications())


if __name__ == "__main__":
    unittest.main()
