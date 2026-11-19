"""Local CQE dashboard and reproducible analysis CLI."""

import argparse
import json
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, replace
from datetime import date
from io import StringIO
from pathlib import Path
from threading import Lock
from typing import Literal

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from core.alert_engine import evaluate_signals, issue_mix, rank_groups, window_bounds
from core.listing_metrics import (
    HORIZON_DAYS, RECOVERED_RATING, RECOVERY_SHARE, RatingBucket, listing_metrics, load_demo_rating_buckets,
)
from core.rag_8d import CaseIndex, read_cases
from core.review_data import load_reviews, parse_reviews, simulated_packaging
from core.tagger import Review, TaggedReview, env_flag, tag_reviews


ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
REVIEWS = ROOT / "data" / "amazon_reviews_mock.csv"
KNOWLEDGE_BASE = ROOT / "data" / "historical_8d_kb.md"
DEMO_RATINGS = ROOT / "data" / "demo_listing_ratings.csv"
MAX_CSV_BYTES = 5 * 1024 * 1024


RatingScope = Literal["demo", "full", "partial"]


@dataclass(frozen=True)
class ReviewSample:
    reviews: list[Review]
    tagged: list[TaggedReview]
    as_of: date
    rating_scope: RatingScope
    simulated_reviews: int = 0


def prepare_sample(
    reviews: list[Review], demo_mode: bool, scope: RatingScope, as_of: date | None = None
) -> ReviewSample:
    cutoff = as_of or max(row.date for row in reviews)
    window_bounds(cutoff)  # Validate the date before calling a configured model.
    included = [row for row in reviews if row.date <= cutoff]
    tagged = tag_reviews([row for row in included if row.stars <= 2], demo_mode)
    return ReviewSample(included, tagged, cutoff, scope)


def sample_report(
    sample: ReviewSample, case_index: CaseIndex, demo_mode: bool, demo_ratings: tuple[RatingBucket, ...]
) -> dict:
    recent, baseline = window_bounds(sample.as_of)
    matches = [
        (signal, case_index.recommend(signal, sample.as_of))
        for signal in evaluate_signals(sample.tagged, sample.as_of)
    ]
    is_demo = sample.rating_scope == "demo"
    return jsonable_encoder({
        "demo_mode": demo_mode,
        "data_source": "demo" if is_demo else "import",
        "rating_scope": sample.rating_scope,
        "has_import": not is_demo,
        "simulated_reviews": sample.simulated_reviews,
        "as_of": sample.as_of,
        "sample_start": min((row.date for row in sample.reviews), default=None),
        "input_rows": len(sample.reviews),
        "total_reviews": len(sample.tagged),
        "unclassified_reviews": sum(item.tag.category is None for item in sample.tagged),
        "windows": {"recent": recent, "baseline": baseline},
        "categories": issue_mix(sample.tagged),
        "rankings": {
            "supplier": rank_groups(sample.tagged, "supplier"), "sku": rank_groups(sample.tagged, "sku"),
        },
        "alerts": [{**asdict(signal), "recommendation": match} for signal, match in matches],
        "listings": listing_metrics(
            sample.reviews, demo_ratings if is_demo else (), recent,
            [signal for signal, match in matches if match is not None], full_ratings=sample.rating_scope != "partial",
        ),
        "listing_scenario": {
            "days": HORIZON_DAYS, "recovery_share": RECOVERY_SHARE, "recovered_rating": RECOVERED_RATING,
        },
    })


class DashboardState:
    def __init__(self, as_of: date | None, demo_mode: bool):
        self._lock = Lock()
        self._demo_mode = demo_mode
        self._cases = CaseIndex(read_cases(KNOWLEDGE_BASE))
        self._demo_ratings = load_demo_rating_buckets(DEMO_RATINGS)
        self._demo_sample = prepare_sample(load_reviews(REVIEWS), demo_mode, "demo", as_of)
        self._imported_report: dict | None = None
        self.report = self._analyze(self._demo_sample)

    def _analyze(self, sample: ReviewSample) -> dict:
        return sample_report(sample, self._cases, self._demo_mode, self._demo_ratings)

    def import_reviews(self, reviews: list[Review], full_ratings: bool) -> dict:
        with self._lock:
            sample = prepare_sample(reviews, self._demo_mode, "full" if full_ratings else "partial")
            self._imported_report = self.report = self._analyze(sample)
            return self.report

    def simulate(self) -> dict:
        with self._lock:
            demo = self._demo_sample
            new_reviews = simulated_packaging(demo.as_of, demo.simulated_reviews)
            sample = replace(
                demo, reviews=demo.reviews + new_reviews,
                tagged=demo.tagged + tag_reviews(new_reviews, demo_mode=True),
                simulated_reviews=demo.simulated_reviews + len(new_reviews),
            )
            report = self._analyze(sample)
            report["has_import"] = self._imported_report is not None
            self._demo_sample = sample
            self.report = report
            return self.report

    def restore_import(self) -> dict | None:
        with self._lock:
            if self._imported_report is not None:
                self.report = self._imported_report
            return self._imported_report


def build_report(as_of: date | None = None, demo_mode: bool | None = None) -> dict:
    if demo_mode is None:
        demo_mode = env_flag("DEMO_MODE", default=True)
    return DashboardState(as_of, demo_mode).report


async def read_uploaded_csv(request: Request) -> list[Review]:
    if request.headers.get("content-type", "").partition(";")[0].strip().lower() != "text/csv":
        raise HTTPException(status_code=415, detail="请发送 CSV 文件（Content-Type: text/csv）。")
    content = bytearray()
    async for chunk in request.stream():
        if len(content) + len(chunk) > MAX_CSV_BYTES:
            raise HTTPException(status_code=413, detail="CSV 超过 5 MiB。请缩小导出范围后重试。")
        content.extend(chunk)
    try:
        decoded = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=422, detail="请将文件另存为 UTF-8 CSV 后重试。") from None
    return await run_in_threadpool(parse_reviews, StringIO(decoded, newline=""))


def plain_text(content: str) -> dict:
    return {"tag": "plain_text", "content": content}


def feishu_card(report: dict) -> dict:
    alerts = report["alerts"]
    elements = []
    for alert in alerts:
        recent, baseline = alert["recent"], alert["baseline"]
        suggestion = alert["recommendation"]
        case = suggestion["case"] if suggestion else None
        lots = "、".join(f"{lot}（{count} 条）" for lot, count in alert["lots"].items())
        lines = [
            f"{alert['level']}｜{alert['title']}",
            f"{alert['sku']} / {alert['supplier']}  ·  批次 {lots}",
            f"近 7 天 {recent['count']}/{recent['sample']} 条（{recent['share']:.1f}%），"
            f"基线 {baseline['count']}/{baseline['sample']} 条（{baseline['share']:.1f}%）",
            f"可参考 {case['case_id']} {case['title']}" if case else "请回查批次、原始评论与检验记录。",
        ]
        elements.append({"tag": "div", "text": plain_text("\n".join(lines))})
    if not elements:
        elements.append({"tag": "div", "text": plain_text("当前窗口无达到阈值的质量预警。")})

    return {
        "simulated": True,
        "payload": {
            "msg_type": "interactive",
            "card": {
                "schema": "2.0",
                "header": {
                    "template": "red" if alerts else "blue",
                    "title": plain_text("苗头性客诉预警"),
                    "subtitle": plain_text(
                        f"{report['as_of']} · {len(alerts)} 条预警 · "
                        + ("示例数据" if report["data_source"] == "demo" else "导入数据")
                    ),
                },
                "body": {"direction": "vertical", "elements": elements},
            },
        },
    }


def create_app(as_of: date | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(web_app: FastAPI):
        web_app.state.dashboard = DashboardState(as_of, env_flag("DEMO_MODE", default=True))
        yield

    web_app = FastAPI(title="Crossborder CQE Sentinel", lifespan=lifespan)
    web_app.mount("/assets", StaticFiles(directory=STATIC), name="assets")

    @web_app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @web_app.get("/api/report")
    def report(request: Request) -> dict:
        return request.app.state.dashboard.report

    @web_app.post("/api/import")
    async def import_csv(request: Request, full_ratings: bool = False) -> dict:
        try:
            reviews = await read_uploaded_csv(request)
            return await run_in_threadpool(request.app.state.dashboard.import_reviews, reviews, full_ratings)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except OverflowError:
            raise HTTPException(status_code=422, detail="CSV 的最新日期过早，无法计算预警窗口。") from None
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None

    @web_app.post("/api/demo-simulate")
    def simulate(request: Request) -> dict:
        return request.app.state.dashboard.simulate()

    @web_app.post("/api/restore-import")
    def restore_import(request: Request) -> dict:
        imported = request.app.state.dashboard.restore_import()
        if imported is None:
            raise HTTPException(status_code=404, detail="本次运行暂无成功导入的 CSV。")
        return imported

    @web_app.get("/api/feishu-preview")
    def preview(request: Request) -> dict:
        return feishu_card(request.app.state.dashboard.report)

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
        uvicorn.run(create_app(args.as_of), host="127.0.0.1", port=8001)


app = create_app()

if __name__ == "__main__":
    main()
