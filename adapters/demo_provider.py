"""Synthetic upstream with real HTTP paging via the local fixture route."""

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Query

from .contracts import Source
from .http_feeds import content_hash
from core.listing_metrics import load_demo_rating_buckets
from core.review_data import load_reviews


def demo_source_router(data: Path) -> APIRouter:
    router = APIRouter(prefix="/demo-source/v1", tags=["Synthetic upstream"])
    reviews = []
    traces = {}
    missed_by_sku = {"SL-Air-CA": [None], "SL-Mini-UK": ["TRACE-NOT-FOUND", "TRACE-MISMATCH"]}
    for row in load_reviews(data / "amazon_reviews_mock.csv"):
        trace_ref = f"TR-{row.lot}"
        traces.setdefault(trace_ref, {
            "trace_ref": trace_ref, "sku": row.sku, "lot": row.lot, "supplier": row.supplier,
            "released_on": (row.date - timedelta(days=5)).isoformat(),
        })
        missing = missed_by_sku.get(row.sku)
        reviews.append({
            "review_id": row.review_id, "listing_sku": row.sku, "posted_on": row.date.isoformat(),
            "rating": row.stars, "body": row.review_text, "brand": row.brand,
            "trace_ref": missing.pop(0) if missing else trace_ref,
        })
    traces["TRACE-MISMATCH"] = {
        **traces["TR-LOT-PU-01"], "trace_ref": "TRACE-MISMATCH",
    }
    for index, bucket in enumerate(load_demo_rating_buckets(data / "demo_listing_ratings.csv"), 1):
        reviews.extend({
            "review_id": f"RATE-{index:02d}-{n:03d}", "listing_sku": bucket.sku,
            "posted_on": bucket.date.isoformat(), "rating": bucket.stars,
            "brand": "Brand-Mock", "body": "", "trace_ref": None,
        } for n in range(bucket.count))
    records = {
        Source.REVIEWS: reviews,
        Source.SHIPMENTS: list(traces.values()),
        Source.INSPECTIONS: json.loads((data / "demo_qms_inspections.json").read_text(encoding="utf-8")),
    }
    versions = {source: content_hash(rows)[:16] for source, rows in records.items()}

    @router.get("/{source}")
    async def page(
        source: Source, limit: int = Query(50, ge=1, le=100), cursor: int = Query(0, ge=0),
        snapshot: str | None = None, fault: Literal["unavailable"] | None = None,
    ) -> dict:
        await asyncio.sleep(0.065 if source is Source.REVIEWS else 0.16)
        if fault:
            raise HTTPException(503, "Synthetic upstream unavailable")
        if snapshot is not None and snapshot != versions[source]:
            raise HTTPException(409, "Snapshot expired")
        rows = records[source]
        end = cursor + limit
        return {
            "schema_version": "1.0", "snapshot_id": versions[source],
            "coverage": "all_ratings" if source is Source.REVIEWS else "records",
            "items": rows[cursor:end], "next_cursor": str(end) if end < len(rows) else None,
        }

    return router
