"""Build the public replay from the same Python workbench used locally."""

import csv
import hashlib
import io
import json
import shutil
import tempfile
from pathlib import Path

import httpx

from adapters.contracts import MarketplaceReview, ShipmentTrace, Source
from adapters.http_feeds import PAGE_SIZE, canonical_json, content_hash
from benchmarks.smoke_http import run, standalone_base
from core.reporting import KNOWLEDGE_BASE
from service.traceability import normalize_reviews


DIRECTORY = Path(__file__).resolve().parent
STATIC = DIRECTORY.parent / "static"
SIMULATIONS = 10


def request(client: httpx.Client, method: str, path: str, **kwargs) -> httpx.Response:
    return client.request(method, path, **kwargs).raise_for_status()


def capture_view(client: httpx.Client, report: dict | None = None) -> dict:
    return {
        "report": report if report is not None else request(client, "GET", "/api/report").json(),
        "card": request(client, "GET", "/api/feishu-preview").json(),
    }


def capture_feeds(client: httpx.Client, job: dict, assets: Path) -> dict:
    feeds = {}
    assets.mkdir(parents=True)
    for status in job["sources"]:
        source = status["source"]
        cursor = None
        rows, pages = [], []
        coverage = None
        for page_no in range(status["pages"]):
            params = {"limit": PAGE_SIZE}
            if cursor is not None:
                params.update(cursor=cursor, snapshot=status["snapshot_id"])
            response = request(client, "GET", f"/demo-source/v1/{source}", params=params)
            page = response.json()
            if page["snapshot_id"] != status["snapshot_id"]:
                raise RuntimeError(f"Fixture {source} changed during build")
            coverage = page["coverage"]
            name = f"{source}-{page_no}.json"
            (assets / name).write_bytes(response.content)
            pages.append({"path": f"/_sandbox/{name}", "sha256": hashlib.sha256(response.content).hexdigest()})
            rows.extend(page["items"])
            cursor = page["next_cursor"]
        if cursor is not None or len(rows) != status["rows"]:
            raise RuntimeError(f"Incomplete fixture {source}")
        feeds[source] = {
            "pages": pages, "rows": rows, "coverage": coverage,
            "snapshot_id": status["snapshot_id"], "sha256": status["sha256"],
        }
    return feeds


def sample_csv(feeds: dict) -> bytes:
    raw = tuple(
        MarketplaceReview.model_validate_json(canonical_json(item))
        for item in feeds[Source.REVIEWS]["rows"]
    )
    shipments = tuple(
        ShipmentTrace.model_validate_json(canonical_json(item))
        for item in feeds[Source.SHIPMENTS]["rows"]
    )
    rows = normalize_reviews(raw, shipments).reviews
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(type(rows[0]).model_fields), lineterminator="\n")
    writer.writeheader()
    writer.writerows(row.model_dump(mode="json") for row in rows)
    return output.getvalue().encode("utf-8")


def build() -> None:
    with tempfile.TemporaryDirectory(dir=DIRECTORY, prefix=".build-") as temp:
        public = Path(temp) / "dist"
        assets = public / "assets"
        assets.mkdir(parents=True)
        shutil.copyfile(STATIC / "index.html", public / "index.html")
        for name in ("dashboard.css", "dashboard.js", "workbench.js"):
            shutil.copyfile(STATIC / name, assets / name)
        shutil.copytree(STATIC / "vendor", assets / "vendor")
        (public / "sources").mkdir()
        shutil.copyfile(KNOWLEDGE_BASE, public / "sources" / KNOWLEDGE_BASE.name)

        with standalone_base() as base, httpx.Client(base_url=base, timeout=8, trust_env=False) as client:
            initial = capture_view(client)
            job = run(client, "normal")
            if job["state"] != "ready":
                raise RuntimeError(f"The build replay did not finish: {job['error']}")
            synced = capture_view(client, request(client, "POST", f"/api/v1/runs/{job['run_id']}/activate").json())
            synced["report"]["sync"].pop("run_id")
            feeds = capture_feeds(client, job, public / "_sandbox")
            csv_bytes = sample_csv(feeds)
            (public / "_sandbox" / "sample-reviews.csv").write_bytes(csv_bytes)
            imports = {}
            for scope in ("false", "true"):
                report = request(
                    client, "POST", f"/api/import?full_ratings={scope}",
                    content=csv_bytes, headers={"Content-Type": "text/csv"},
                ).json()
                imports[scope] = capture_view(client, report)
            simulations = [
                capture_view(client, request(client, "POST", "/api/demo-simulate").json())
                for _ in range(SIMULATIONS)
            ]
            contracts = request(client, "GET", "/api/v1/contracts").json()
            openapi = request(client, "GET", "/openapi.json").json()

        openapi["info"]["title"] = "Crossborder CQE Sentinel · Cloudflare 演示 API"
        openapi["info"]["description"] = "公开会话运行合成三源回放，CSV 演示接受内置样例。"
        bundle = {
            "initial": initial, "synced": synced, "feeds": feeds,
            "imports": imports, "simulations": simulations, "contracts": contracts,
            "sample_csv": {
                "url": "/demo-source/sample-reviews.csv", "bytes": len(csv_bytes),
                "sha256": hashlib.sha256(csv_bytes).hexdigest(),
            },
        }
        bundle["version"] = content_hash(bundle)[:16]
        (public / "openapi.json").write_text(json.dumps(openapi, ensure_ascii=False), encoding="utf-8")
        (Path(temp) / "generated.json").write_text(canonical_json(bundle), encoding="utf-8")
        destination = DIRECTORY / "dist"
        if destination.exists():
            shutil.rmtree(destination)
        public.replace(destination)
        (Path(temp) / "generated.json").replace(DIRECTORY / "generated.json")
        print(f"Cloudflare sandbox {bundle['version']}: 408 ratings, 3 sources, {SIMULATIONS} simulation steps.")


if __name__ == "__main__":
    build()
