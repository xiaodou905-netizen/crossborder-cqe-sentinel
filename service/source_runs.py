"""Coordinate concurrent feeds and publish complete analysis snapshots."""

import asyncio
import logging
from dataclasses import asdict
from time import perf_counter

import httpx
from starlette.concurrency import run_in_threadpool

from adapters.contracts import Source
from adapters.http_feeds import FeedAdapter, FeedResult, Scenario, SourceFailure, SourceStatus, content_hash
from core.reporting import Analyzer

from .run_models import RunState, RunView
from .run_store import RunStore
from .traceability import attach_quality_evidence, normalize_reviews


LOGGER = logging.getLogger(__name__)
FETCH_DEADLINE = 24.0


class SourceRuns:
    def __init__(
        self, store: RunStore, analyzer: Analyzer, adapters: tuple[FeedAdapter, ...], synthetic: bool,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.store = store
        self.analyzer = analyzer
        self.adapters = adapters
        self.synthetic = synthetic
        self.transport = transport
        self.task: asyncio.Task | None = None

    def start(self, scenario: Scenario, key: str | None) -> RunView:
        run, created = self.store.create(key, scenario, self.synthetic)
        if created:
            self.task = asyncio.create_task(self._run(run))
        return run

    async def shutdown(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    async def _pull(
        self, client: httpx.AsyncClient, adapter: FeedAdapter, run: RunView
    ) -> FeedResult | None:
        self.store.update(run.run_id, source=SourceStatus(adapter.source, "running"))
        fault = run.scenario == "trace_unavailable" and adapter.source is Source.SHIPMENTS
        try:
            result = await adapter.read(client, fault=fault)
        except SourceFailure as exc:
            self.store.update(run.run_id, source=exc.status)
            return None
        self.store.update(run.run_id, source=result.status)
        return result

    def _analyze(self, run_id: str, feeds: dict[Source, FeedResult]) -> dict:
        reviews = feeds[Source.REVIEWS]
        traced = normalize_reviews(reviews.items, feeds[Source.SHIPMENTS].items)
        sample = self.analyzer.prepare(
            traced.reviews, "full" if reviews.coverage == "all_ratings" else "partial", "sync",
        )
        report = self.analyzer.report(sample)
        attach_quality_evidence(report, traced, feeds[Source.INSPECTIONS].items)
        report["sync"] = {
            "run_id": run_id, "profile": "synthetic_http" if self.synthetic else "configured_http",
            "traceability": traced.summary,
            "trace_rejections": [asdict(item) for item in traced.rejections],
            "input_sha256": content_hash({name.value: feed.status.sha256 for name, feed in feeds.items()}),
        }
        return report

    async def _run(self, run: RunView) -> None:
        started = perf_counter()
        self.store.update(run.run_id, RunState.FETCHING)
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(6, connect=2.5), follow_redirects=False, trust_env=False, transport=self.transport,
            ) as client, asyncio.timeout(FETCH_DEADLINE):
                results = await asyncio.gather(*(self._pull(client, adapter, run) for adapter in self.adapters))
            if any(result is None for result in results):
                self.store.update(run.run_id, RunState.FAILED, error="至少一条来源失败。已成功发布的快照可继续使用。",
                                  wall_ms=round((perf_counter() - started) * 1000))
                return
            self.store.update(run.run_id, RunState.ANALYZING)
            feeds = {result.status.source: result for result in results if result is not None}
            report = await run_in_threadpool(self._analyze, run.run_id, feeds)
            self.store.update(run.run_id, RunState.READY, report=report,
                              wall_ms=round((perf_counter() - started) * 1000))
        except asyncio.CancelledError:
            self.store.update(run.run_id, RunState.FAILED, error="同步因服务关闭而中断。")
            raise
        except TimeoutError:
            self.store.update(run.run_id, RunState.FAILED, error="来源同步超出总时间预算。")
        except Exception as exc:
            LOGGER.error("Source run %s failed with %s", run.run_id, type(exc).__name__)
            self.store.update(run.run_id, RunState.FAILED, error="分析未完成，请检查来源契约和模型配置。")
