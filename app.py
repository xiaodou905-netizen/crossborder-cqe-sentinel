"""Local CQE dashboard and reproducible analysis CLI."""

import argparse
import csv
import json
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import date
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from core.alert_engine import evaluate_signals, issue_mix, rank_groups, window_bounds
from core.rag_8d import CaseIndex, read_cases
from core.tagger import Review, env_flag, tag_reviews


ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
REVIEWS = ROOT / "data" / "amazon_reviews_mock.csv"
KNOWLEDGE_BASE = ROOT / "data" / "historical_8d_kb.md"


def load_reviews(path: Path) -> list[Review]:
    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames is None or sorted(reader.fieldnames) != sorted(Review.model_fields):
            raise ValueError(f"{path.name} 的列名与 README 中的 CSV 格式不一致。")

        ids = set()
        rows = []
        for values in reader:
            try:
                row = Review.model_validate(values)
            except ValidationError as exc:
                fields = ", ".join(str(error["loc"][0]) for error in exc.errors())
                raise ValueError(f"{path.name} 第 {reader.line_num} 行字段无效（{fields}）。") from None
            if row.review_id in ids:
                raise ValueError(f"{path.name} 第 {reader.line_num} 行的评论编号重复。")
            ids.add(row.review_id)
            rows.append(row)
    return rows


def build_report(as_of: date | None = None, demo_mode: bool | None = None) -> dict:
    if demo_mode is None:
        demo_mode = env_flag("DEMO_MODE", default=True)
    reviews = [row for row in load_reviews(REVIEWS) if row.stars <= 2]
    if as_of is None:
        as_of = max((row.date for row in reviews), default=date.today())
    reviews = [row for row in reviews if row.date <= as_of]
    tagged = tag_reviews(reviews, demo_mode)
    recent, baseline = window_bounds(as_of)
    case_index = CaseIndex(read_cases(KNOWLEDGE_BASE))
    alerts = [
        {**asdict(signal), "recommendation": case_index.recommend(signal, as_of)}
        for signal in evaluate_signals(tagged, as_of)
    ]
    return jsonable_encoder({
        "demo_mode": demo_mode,
        "as_of": as_of,
        "sample_start": min((row.date for row in reviews), default=None),
        "total_reviews": len(tagged),
        "unclassified_reviews": sum(item.tag.category is None for item in tagged),
        "windows": {"recent": recent, "baseline": baseline},
        "categories": issue_mix(tagged),
        "rankings": {"supplier": rank_groups(tagged, "supplier"), "sku": rank_groups(tagged, "sku")},
        "alerts": alerts,
    })


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
                    "subtitle": plain_text(f"{report['as_of']} · {len(alerts)} 条预警"),
                },
                "body": {"direction": "vertical", "elements": elements},
            },
        },
    }


def create_app(as_of: date | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(web_app: FastAPI):
        web_app.state.report = build_report(as_of)
        yield

    web_app = FastAPI(title="Crossborder CQE Sentinel", lifespan=lifespan)
    web_app.mount("/assets", StaticFiles(directory=STATIC), name="assets")

    @web_app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @web_app.get("/api/report")
    def report(request: Request) -> dict:
        return request.app.state.report

    @web_app.get("/api/feishu-preview")
    def preview(request: Request) -> dict:
        return feishu_card(request.app.state.report)

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
