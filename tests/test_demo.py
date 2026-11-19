import json
import os
import unittest
from datetime import date, timedelta
from unittest.mock import patch

import httpx

from app import KNOWLEDGE_BASE, REVIEWS, build_report, create_app, load_reviews
from core.alert_engine import Level, evaluate_signals
from core.rag_8d import CaseIndex, read_cases
from core.tagger import Category, Tag, TaggedReview, classify_compatible


END_DATE = date(2026, 11, 19)


class DemoReplayTests(unittest.TestCase):
    def test_two_planted_alerts_match_history(self):
        report = build_report(demo_mode=True)
        self.assertEqual(report["as_of"], END_DATE.isoformat())
        self.assertEqual(report["total_reviews"], 80)
        self.assertEqual(report["unclassified_reviews"], 0)
        self.assertEqual([row["count"] for row in report["categories"]], [18, 19, 27, 16])
        self.assertEqual(len(read_cases(KNOWLEDGE_BASE)), 5)
        self.assertEqual(len(report["alerts"]), 2)

        packing, fingerprint = report["alerts"]
        self.assertEqual((packing["sku"], packing["supplier"], packing["category"], packing["level"]),
                         ("SL-Pro-US", "Supplier-A", "包装破损", "重点预警"))
        self.assertEqual((packing["recent"]["count"], packing["recent"]["sample"]), (8, 12))
        self.assertEqual((packing["baseline"]["count"], packing["baseline"]["sample"]), (1, 24))
        self.assertEqual(packing["lots"], {"LOT-PU-04": 8})
        self.assertEqual(packing["recommendation"]["case"]["case_id"], "8D-002")
        burst = [row for row in load_reviews(REVIEWS) if row.lot == "LOT-PU-04"]
        self.assertEqual(len(burst), 8)
        self.assertTrue(all(END_DATE - timedelta(days=4) <= row.date <= END_DATE for row in burst))

        self.assertEqual((fingerprint["sku"], fingerprint["supplier"], fingerprint["category"], fingerprint["level"]),
                         ("SL-Max-EU", "Supplier-B", "功能故障", "苗头关注"))
        self.assertEqual((fingerprint["recent"]["count"], fingerprint["recent"]["sample"]), (3, 8))
        self.assertEqual((fingerprint["baseline"]["count"], fingerprint["baseline"]["sample"]), (0, 17))
        self.assertEqual(fingerprint["recommendation"]["case"]["case_id"], "8D-004")

    def test_replay_excludes_future_reviews(self):
        report = build_report(date(2026, 11, 12), demo_mode=True)
        self.assertEqual(report["total_reviews"], 55)
        self.assertEqual(report["alerts"], [])

    def test_novel_issue_respects_sample_floor_and_can_have_no_match(self):
        review = load_reviews(REVIEWS)[0]
        unknown = Tag(category=None, issue=None, keywords=[])
        symptom = Tag(category=Category.FUNCTION, issue=None, keywords=["motor gearbox stalling"])
        recent = [TaggedReview(review.model_copy(update={"date": END_DATE}), tag)
                  for tag in [symptom] * 3 + [unknown] * 3]
        baseline = [TaggedReview(review.model_copy(update={"date": END_DATE - timedelta(days=7)}), unknown)] * 10

        self.assertEqual(evaluate_signals(recent + baseline[:9], END_DATE), [])
        signal, = evaluate_signals(recent + baseline, END_DATE)
        self.assertEqual(signal.level, Level.EARLY)
        self.assertIsNone(CaseIndex(read_cases(KNOWLEDGE_BASE)).recommend(signal, END_DATE))

class DashboardTests(unittest.IsolatedAsyncioTestCase):
    async def test_preview_contains_both_alerts(self):
        app = create_app()
        with patch.dict(os.environ, {"DEMO_MODE": "1"}):
            async with app.router.lifespan_context(app), httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                self.assertEqual((await client.get("/")).status_code, 200)
                self.assertEqual((await client.get("/api/report")).json()["total_reviews"], 80)
                preview = (await client.get("/api/feishu-preview")).json()
        self.assertTrue(preview["simulated"])
        self.assertEqual(preview["payload"]["msg_type"], "interactive")
        card = preview["payload"]["card"]
        self.assertEqual(card["header"]["template"], "red")
        self.assertIn("8D-002", card["body"]["elements"][0]["text"]["content"])
        self.assertIn("8D-004", card["body"]["elements"][1]["text"]["content"])


class CompatibleApiTests(unittest.TestCase):
    def test_chat_completion_contract_and_validation(self):
        review = load_reviews(REVIEWS)[0].model_copy(update={"review_text": "The crushed inner tray had ripped."})
        valid = {"review_id": review.review_id, "category": "包装破损", "issue": "inner_tray_crush",
                 "keywords": ["crushed inner tray"]}

        def reply(request):
            self.assertEqual(request.url.path, "/v1/chat/completions")
            request_body = json.loads(request.content)
            self.assertEqual(request_body["response_format"], {"type": "json_object"})
            self.assertEqual(json.loads(request_body["messages"][1]["content"]), [
                {"review_id": review.review_id, "review_text": review.review_text},
            ])
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"results": [item]})}}]})

        with httpx.Client(base_url="https://localhost/v1/", transport=httpx.MockTransport(reply)) as client:
            item = valid
            result, = classify_compatible([review], client, "mock-model", json_mode=True)
            self.assertEqual(result.tag.category, Category.PACKAGING)
            for overrides in ({"category": "物流损坏"}, {"review_id": "wrong-id"}, {"keywords": ["made up"]}):
                with self.subTest(overrides=overrides):
                    item = {**valid, **overrides}
                    with self.assertRaises(RuntimeError):
                        classify_compatible([review], client, "mock-model", json_mode=True)


if __name__ == "__main__":
    unittest.main()
