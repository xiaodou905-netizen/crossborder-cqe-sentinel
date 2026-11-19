"""Compose the CQE dashboard, source workbench, and reproducible CLI."""

import argparse
import json
import os
from contextlib import asynccontextmanager
from datetime import date
from io import StringIO
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from adapters.demo_provider import demo_source_router
from adapters.http_feeds import configured_adapters
from core.reporting import DATA, KNOWLEDGE_BASE, Analyzer
from core.review_data import parse_reviews
from core.tagger import Review, env_flag
from service.api import router
from service.feishu import feishu_card
from service.run_store import RunStore
from service.source_runs import SourceRuns
from service.workbench import Workbench


ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
MAX_CSV_BYTES = 5 * 1024 * 1024


def listening_port() -> int:
    return int(os.getenv("PORT", "8001"))


def build_report(as_of: date | None = None, demo_mode: bool | None = None) -> dict:
    if demo_mode is None:
        demo_mode = env_flag("DEMO_MODE", default=True)
    analyzer = Analyzer(demo_mode)
    return analyzer.report(analyzer.prepare_demo(as_of))


async def read_uploaded_csv(request: Request) -> list[Review]:
    if request.headers.get("content-type", "").partition(";")[0].strip().lower() != "text/csv":
        raise HTTPException(415, "请发送 CSV 文件（Content-Type: text/csv）。")
    content = bytearray()
    async for chunk in request.stream():
        if len(content) + len(chunk) > MAX_CSV_BYTES:
            raise HTTPException(413, "CSV 超过 5 MiB。请缩小导出范围后重试。")
        content.extend(chunk)
    try:
        decoded = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(422, "请将文件另存为 UTF-8 CSV 后重试。") from None
    return await run_in_threadpool(parse_reviews, StringIO(decoded, newline=""))


def require_local_write(request: Request) -> None:
    if request.app.state.public_demo:
        raise HTTPException(403, "此操作仅在本地工作台开放。")


def current_report(request: Request) -> dict:
    report = request.app.state.dashboard.report
    if report["data_source"] == "sync":
        current = request.app.state.runs.store.report(report["sync"]["run_id"])
        return {**current, "has_import": report["has_import"]}
    return report


def create_app(
    as_of: date | None = None, *, db_path: str | Path | None = None,
    source_transport: httpx.AsyncBaseTransport | None = None, public_demo: bool | None = None,
) -> FastAPI:
    if public_demo is None:
        public_demo = env_flag("SENTINEL_PUBLIC_DEMO", default=False)
    adapters, synthetic = configured_adapters(listening_port(), public_demo)
    store_file = ROOT / ".sentinel" / "workbench.sqlite3"
    store_path = db_path or (":memory:" if public_demo else (os.getenv("SENTINEL_DB_PATH") or store_file))

    @asynccontextmanager
    async def lifespan(web_app: FastAPI):
        analyzer = Analyzer(public_demo or env_flag("DEMO_MODE", default=True))
        web_app.state.dashboard = Workbench(as_of, analyzer)
        web_app.state.public_demo = public_demo
        store = RunStore(store_path)
        runs = SourceRuns(store, analyzer, adapters, synthetic, source_transport)
        web_app.state.runs = runs
        try:
            recent = store.latest(ready_only=True)
            if as_of is None and recent is not None:
                report = store.report(recent.run_id)
                if report is not None and (not public_demo or recent.profile == "synthetic_http"):
                    web_app.state.dashboard.show_sync(report)
            yield
        finally:
            await runs.shutdown()
            store.close()

    web_app = FastAPI(title="Crossborder CQE Sentinel", version="1.0.0", lifespan=lifespan)
    web_app.mount("/assets", StaticFiles(directory=STATIC), name="assets")
    web_app.include_router(router)
    if synthetic:
        web_app.include_router(demo_source_router(DATA))

    @web_app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @web_app.get("/healthz")
    def health() -> dict:
        return {"status": "ok"}

    @web_app.get("/api/report")
    def report(request: Request) -> dict:
        return current_report(request)

    @web_app.post("/api/import")
    async def import_csv(request: Request, full_ratings: bool = False) -> dict:
        require_local_write(request)
        try:
            reviews = await read_uploaded_csv(request)
            return await run_in_threadpool(request.app.state.dashboard.import_reviews, reviews, full_ratings)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except OverflowError:
            raise HTTPException(422, "CSV 的最新日期过早，无法计算预警窗口。") from None
        except RuntimeError as exc:
            raise HTTPException(502, str(exc)) from None

    @web_app.post("/api/demo-simulate")
    def simulate(request: Request) -> dict:
        require_local_write(request)
        return request.app.state.dashboard.simulate()

    @web_app.post("/api/restore-import")
    def restore_import(request: Request) -> dict:
        require_local_write(request)
        imported = request.app.state.dashboard.restore_import()
        if imported is None:
            raise HTTPException(404, "本次运行暂无成功导入的 CSV。")
        return imported

    @web_app.get("/api/feishu-preview")
    def preview(request: Request) -> dict:
        return feishu_card(current_report(request))

    @web_app.get("/sources/historical_8d_kb.md", include_in_schema=False)
    def read_knowledge_base() -> FileResponse:
        return FileResponse(KNOWLEDGE_BASE, media_type="text/plain")

    return web_app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analyze", action="store_true", help="Print the report as JSON and exit")
    parser.add_argument("--as-of", type=date.fromisoformat, metavar="YYYY-MM-DD", help="Replay up to this day")
    args = parser.parse_args()
    if args.analyze:
        try:
            report = build_report(args.as_of)
        except (ValueError, OSError, RuntimeError) as exc:
            parser.exit(status=1, message=f"{exc}\n")
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        uvicorn.run(
            app if args.as_of is None else create_app(args.as_of),
            host=os.getenv("SENTINEL_HOST", "127.0.0.1"), port=listening_port(),
        )


app = create_app()

if __name__ == "__main__":
    main()
