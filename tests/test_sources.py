import asyncio
import os
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from time import monotonic
from unittest.mock import patch

import httpx
from fastapi import FastAPI

from adapters.contracts import FeedPage, ShipmentTrace, Source
from adapters.demo_provider import demo_source_router
from adapters.http_feeds import FeedAdapter, SourceFailure
from app import create_app
from core.reporting import DATA
from service.run_models import RunState
from service.run_store import RunStore


@asynccontextmanager
async def workbench_client(public_demo=False):
    upstream = FastAPI()
    upstream.include_router(demo_source_router(DATA))
    performance = {"active": 0, "max_in_flight": 0}

    @upstream.middleware("http")
    async def observe(request, call_next):
        performance["active"] += 1
        performance["max_in_flight"] = max(performance["max_in_flight"], performance["active"])
        try:
            return await call_next(request)
        finally:
            performance["active"] -= 1

    variables = {f"SENTINEL_{source.value.upper()}_URL": "" for source in Source}
    with patch.dict(os.environ, {"DEMO_MODE": "1", **variables}):
        backend = create_app(
            db_path=":memory:", public_demo=public_demo, source_transport=httpx.ASGITransport(app=upstream),
        )
        async with backend.router.lifespan_context(backend), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=backend), base_url="http://testserver",
        ) as client:
            yield client, performance


async def completed(client, run_id):
    deadline = monotonic() + 5
    while monotonic() < deadline:
        response = await client.get(f"/api/v1/runs/{run_id}")
        response.raise_for_status()
        run = response.json()
        if run["state"] in {"ready", "failed"}:
            return run
        await asyncio.sleep(.025)
    raise AssertionError("source run did not finish")


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_paged_adapter_checks_snapshot_and_conflicting_identity(self):
        adapter = FeedAdapter(Source.SHIPMENTS, "https://fixture.test/feed", FeedPage[ShipmentTrace], "trace_ref")
        entry = dict(trace_ref="TR-01", sku="SKU-X", lot="LOT-X", supplier="Supplier-X", released_on="2026-11-10")
        for failure, attempted_change in (
            ("游标循环", {"next_cursor": "again"}),
            ("快照", {"snapshot_id": "second"}),
            ("不同内容", {"items": [{**entry, "lot": "LOT-DIFFERENT"}]}),
        ):
            with self.subTest(failure=failure):
                def handler(request):
                    second = request.url.params.get("cursor") == "again"
                    if second:
                        self.assertEqual(request.url.params.get("snapshot"), "first")
                    request_page = {
                        "schema_version": "1.0", "snapshot_id": "first", "coverage": "records",
                        "items": [entry], "next_cursor": None if second else "again",
                    }
                    if second:
                        request_page.update(attempted_change)
                    return httpx.Response(200, json=request_page)
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    with self.assertRaisesRegex(SourceFailure, failure):
                        await adapter.read(client)


class SourceWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_sync_idempotency_and_preserving_last_good_on_failed_source(self):
        async with workbench_client() as (client, performance):
            headers = {"Idempotency-Key": "first"}
            started = await client.post("/api/v1/runs", json={}, headers=headers)
            self.assertEqual(started.status_code, 202)
            run_id = started.json()["run_id"]
            repeated = await client.post("/api/v1/runs", json={}, headers=headers)
            self.assertEqual(repeated.json()["run_id"], run_id)
            self.assertEqual((await client.post("/api/v1/runs", json={"scenario":"trace_unavailable"},
                                                headers=headers)).status_code, 409)
            good = await completed(client, run_id)
            self.assertEqual(good["state"], "ready")
            self.assertEqual(performance["max_in_flight"], 3)
            self.assertEqual([(s["rows"], s["pages"]) for s in good["sources"]], [(408, 9), (13, 1), (4, 1)])
            snapshot = (await client.post(f"/api/v1/runs/{run_id}/activate")).json()
            self.assertEqual(snapshot["sync"]["traceability"], {"linked": 77, "missing": 1, "unmatched": 1, "conflict": 1})
            packing, fingerprint = snapshot["alerts"]
            self.assertEqual(packing["quality_evidence"]["stage"], "reproduced")
            self.assertEqual([record["inspection_id"] for record in packing["quality_evidence"]["inspections"]],
                             ["QMS-26-117"])
            self.assertEqual(fingerprint["quality_evidence"]["stage"], "inspection")
            self.assertEqual([record["inspection_id"] for record in fingerprint["quality_evidence"]["inspections"]],
                             ["QMS-26-123"])
            for record in packing["evidence"]:
                self.assertTrue(record["trace_ref"])
                for span in record["spans"]:
                    self.assertEqual(record["review_text"][span["start"]:span["end"]], span["quote"])

            fault = (await client.post("/api/v1/runs", json={"scenario": "trace_unavailable"})).json()
            failed = await completed(client, fault["run_id"])
            self.assertEqual(failed["state"], "failed")
            self.assertEqual(failed["sources"][1]["retries"], 2)
            self.assertEqual((await client.get(f"/api/v1/runs/{fault['run_id']}/report")).status_code, 409)
            self.assertEqual((await client.get("/api/report")).json(), snapshot)
            self.assertEqual((await client.get("/api/v1/workbench")).json()["latest_ready"]["run_id"], run_id)

            rerun = (await client.post("/api/v1/runs", json={})).json()
            repeated_input = await completed(client, rerun["run_id"])
            self.assertEqual(repeated_input["input_sha256"], good["input_sha256"])

    async def test_handoff_requires_qms_reproduction_and_rejects_stale_revision(self):
        async with workbench_client() as (client, _):
            run_id = (await client.post("/api/v1/runs", json={})).json()["run_id"]
            run = await completed(client, run_id)
            packing, fingerprint = (await client.get(run["report_path"])).json()["alerts"]
            for alert, expected in [(packing, 200), (fingerprint, 409)]:
                path = f"/api/v1/runs/{run_id}/cases/{alert['case_key']}/triage"
                start = {"expected_revision": 0, "next_phase": "investigating", "note": "核对批次与现场记录"}
                self.assertEqual((await client.post(path, json=start)).status_code, 200)
                self.assertEqual((await client.post(path, json=start)).status_code, 409)
                handoff = {"expected_revision": 1, "next_phase": "verification_requested", "note": "移交实验室做对照复验"}
                response = await client.post(path, json=handoff)
                self.assertEqual(response.status_code, expected)
                if expected == 200:
                    self.assertEqual([event["revision"] for event in response.json()["history"]], [1, 2])

    async def test_public_mode_limits_writes_and_reuses_completed_run(self):
        async with workbench_client(public_demo=True) as (client, _):
            self.assertEqual((await client.post("/api/import")).status_code, 403)
            self.assertEqual((await client.post("/api/demo-simulate")).status_code, 403)
            self.assertEqual((await client.post("/api/v1/runs", json={"scenario": "trace_unavailable"})).status_code, 422)
            run_id = (await client.post("/api/v1/runs", json={})).json()["run_id"]
            run = await completed(client, run_id)
            replay = await client.post("/api/v1/runs", json={}, headers={"Idempotency-Key":"different"})
            self.assertEqual(replay.json()["run_id"], run_id)
            case = (await client.get(run["report_path"])).json()["alerts"][0]
            body = {"expected_revision": 0, "next_phase":"investigating", "note":"这条记录不应提交"}
            self.assertEqual((await client.post(
                f"/api/v1/runs/{run_id}/cases/{case['case_key']}/triage", json=body,
            )).status_code, 403)


class StoreRecoveryTests(unittest.TestCase):
    def test_restart_marks_nonterminal_run_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.sqlite3"
            first = RunStore(path)
            run, _ = first.create("restart-test", "normal", True)
            first.update(run.run_id, RunState.FETCHING)
            first.close()
            resumed = RunStore(path)
            try:
                self.assertEqual(resumed.get(run.run_id).state, RunState.FAILED)
                self.assertIsNone(resumed.report(run.run_id))
                self.assertIsNone(resumed.latest(ready_only=True))
            finally:
                resumed.close()
