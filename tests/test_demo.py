import json
import os
import unittest
from contextlib import asynccontextmanager
from datetime import date, timedelta
from unittest.mock import patch

import httpx

from app import build_report, create_app
from core.alert_engine import Level, evaluate_signals
from core.rag_8d import CaseIndex, read_cases
from core.reporting import KNOWLEDGE_BASE, REVIEWS
from core.review_data import load_reviews
from core.tagger import Category, Tag, TaggedReview, classify_compatible


END_DATE = date(2026, 11, 19)
CSV_HEADERS = {"Content-Type": "text/csv"}
IMPORT_SAMPLE = (
    "review_id,date,sku,stars,review_text,ignored_field\r\n"
    'item-01,2026-10-31,SKU-Demo-Import,1,"The crushed inner tray,\r\nunder the cover.",x\r\n'
    "item-02,2026-11-01,SKU-Demo-Import,2,,x\r\n"
    "item-03,2026-11-01,SKU-Demo-Import,5,,x\r\n"
    "item-04,2026-11-02,SKU-Demo-Import,4,Works fine.,x\r\n"
)


@asynccontextmanager
async def dashboard_client():
    app = create_app(db_path=":memory:")
    with patch.dict(os.environ, {"DEMO_MODE": "1"}):
        async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            yield client


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

    def test_demo_listing_lift_uses_rating_counts_and_only_linked_signals(self):
        metrics = {row["sku"]: row for row in build_report(demo_mode=True)["listings"]}
        pro = metrics["SL-Pro-US"]
        self.assertEqual(pro["rating_count"], 154)
        self.assertAlmostEqual(pro["current_stars"], 629 / 154, places=3)
        self.assertEqual(pro["recent_rating_count"], 21)
        self.assertEqual(pro["linked_recent_reviews"], 8)
        expected_lift = (8 * 5 - 11) * (30 / 7) * 0.7 / (154 + 21 * (30 / 7))
        self.assertAlmostEqual(pro["lift_stars"], expected_lift, places=3)
        self.assertIsNone(metrics["SL-Mini-UK"]["lift_stars"])

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
        async with dashboard_client() as client:
            self.assertEqual((await client.get("/")).status_code, 200)
            self.assertEqual((await client.get("/api/report")).json()["total_reviews"], 80)
            preview = (await client.get("/api/feishu-preview")).json()
        self.assertTrue(preview["simulated"])
        self.assertEqual(preview["payload"]["msg_type"], "interactive")
        card = preview["payload"]["card"]
        self.assertEqual(card["header"]["template"], "red")
        self.assertIn("8D-002", card["body"]["elements"][0]["text"]["content"])
        self.assertIn("8D-004", card["body"]["elements"][1]["text"]["content"])

    async def test_upload_scope_switches_star_metrics_and_isolated_simulation_updates_preview(self):
        async with dashboard_client() as client:
            content = IMPORT_SAMPLE.encode("utf-8-sig")
            response = await client.post("/api/import", headers=CSV_HEADERS, content=content)
            self.assertEqual(response.status_code, 200)
            partial = response.json()
            self.assertEqual((partial["input_rows"], partial["total_reviews"], partial["unclassified_reviews"]), (4, 2, 1))
            self.assertEqual(partial["as_of"], "2026-11-02")
            self.assertEqual(partial["rankings"]["sku"][0]["name"], "SKU-Demo-Import")
            self.assertEqual(partial["rankings"]["supplier"][0]["name"], "未标注")
            self.assertIsNone(partial["listings"][0]["current_stars"])

            response = await client.post("/api/import?full_ratings=true", headers=CSV_HEADERS, content=content)
            self.assertEqual(response.status_code, 200)
            imported = response.json()
            self.assertEqual(imported["listings"][0]["current_stars"], 3.0)
            self.assertIsNone(imported["listings"][0]["lift_stars"])

            simulated = (await client.post("/api/demo-simulate")).json()
            self.assertEqual((simulated["data_source"], simulated["total_reviews"], simulated["simulated_reviews"]),
                             ("demo", 85, 5))
            self.assertTrue(simulated["has_import"])
            self.assertEqual(simulated["alerts"][0]["recent"]["share"], 76.5)
            self.assertEqual(simulated["rankings"]["supplier"][0]["count"], 41)
            self.assertEqual(simulated["categories"][1]["count"], 24)
            self.assertIn("76.5%", json.dumps((await client.get("/api/feishu-preview")).json(), ensure_ascii=False))
            self.assertEqual((await client.post("/api/restore-import")).json(), imported)
            self.assertEqual((await client.get("/api/report")).json(), imported)

            repeated = (await client.post("/api/demo-simulate")).json()
            self.assertEqual(repeated["total_reviews"], 90)
            ids = {row["review_id"] for row in repeated["alerts"][0]["evidence"]}
            self.assertEqual(len(ids), 3)
            self.assertEqual((await client.post("/api/restore-import")).json(), imported)

    async def test_bad_csv_leaves_current_report_intact(self):
        async with dashboard_client() as client:
            before = (await client.get("/api/report")).json()
            for content, reason in (
                (b"wrong,headers\n1,2\n", "缺少必填列"),
                (IMPORT_SAMPLE.replace("item-02", "item-01").encode(), "评论编号重复"),
                (IMPORT_SAMPLE.replace("2026-11-01,SKU-Demo-Import,5", "2026-11-01,SKU-Demo-Import,9").encode(), "stars"),
                (b"\xff", "UTF-8"),
            ):
                with self.subTest(reason=reason):
                    response = await client.post("/api/import", headers=CSV_HEADERS, content=content)
                    self.assertEqual(response.status_code, 422)
                    self.assertIn(reason, response.json()["detail"])
            self.assertEqual((await client.get("/api/report")).json(), before)


class CompatibleApiTests(unittest.TestCase):
    def test_known_mode_abstains_when_literal_quote_does_not_support_failure(self):
        review = load_reviews(REVIEWS)[0].model_copy(update={
            "review_text": "The fingerprint reader worked first time in freezing weather.",
        })
        result = {
            "review_id": review.review_id, "issue": "cold_fingerprint",
            "category": "功能故障", "keywords": ["fingerprint reader", "freezing"],
        }

        def reply(_request):
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"results": [result]})}}]})

        with httpx.Client(base_url="https://localhost/v1/", transport=httpx.MockTransport(reply)) as client:
            tagged, = classify_compatible([review], client, "mock-model", json_mode=True)
        self.assertIsNone(tagged.tag.issue)
        self.assertIsNone(tagged.tag.category)

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
