"""Associate customer evidence with MES and QMS records without guessing joins."""

from collections import Counter
from dataclasses import dataclass
from datetime import date
from typing import Literal

from adapters.contracts import Inspection, MarketplaceReview, ShipmentTrace
from adapters.http_feeds import content_hash
from core.tagger import Review


UNMAPPED = "未标注"
TRACE_REJECTION_LIMIT = 8


@dataclass(frozen=True)
class TraceRejection:
    review_id: str
    sku: str
    trace_ref: str | None
    reason: Literal["missing_reference", "unknown_reference", "sku_mismatch", "shipment_after_review"]


@dataclass(frozen=True)
class TracedReviews:
    reviews: list[Review]
    attribution: dict[str, str]
    summary: dict[str, int]
    rejections: list[TraceRejection]


def normalize_reviews(
    raw: tuple[MarketplaceReview, ...], shipments: tuple[ShipmentTrace, ...]
) -> TracedReviews:
    by_ref = {item.trace_ref: item for item in shipments}
    totals = Counter({"linked": 0, "missing": 0, "unmatched": 0, "conflict": 0})
    reviews, links = [], {}
    rejections = []
    for item in raw:
        trace = by_ref.get(item.trace_ref)
        if item.trace_ref is None:
            status, reason = "missing", "missing_reference"
        elif trace is None:
            status, reason = "unmatched", "unknown_reference"
        elif trace.sku != item.listing_sku:
            status, reason = "conflict", "sku_mismatch"
        elif trace.released_on > item.posted_on:
            status, reason = "conflict", "shipment_after_review"
        else:
            status, reason = "linked", None
        if item.rating <= 2:
            totals[status] += 1
            if reason is not None and len(rejections) < TRACE_REJECTION_LIMIT:
                rejections.append(TraceRejection(item.review_id, item.listing_sku, item.trace_ref, reason))
        if status != "linked":
            trace = None
        if trace is not None:
            links[item.review_id] = trace.trace_ref
        reviews.append(Review(
            review_id=item.review_id, date=item.posted_on, brand=item.brand,
            sku=item.listing_sku, stars=item.rating, review_text=item.body,
            supplier=trace.supplier if trace else UNMAPPED, lot=trace.lot if trace else UNMAPPED,
        ))
    return TracedReviews(reviews, links, dict(totals), rejections)


def attach_quality_evidence(report: dict, traced: TracedReviews, inspections: tuple[Inspection, ...]) -> None:
    as_of = date.fromisoformat(report["as_of"])
    for alert in report["alerts"]:
        sku, issue = alert["sku"], alert["issue"]
        lot_refs = set(alert["lots"]) - {UNMAPPED}
        matches = [
            item for item in inspections if (
                item.sku == sku and item.lot in lot_refs and item.issue == issue and item.inspected_on <= as_of
            )
        ]
        matches.sort(key=lambda item: (item.inspected_on, item.inspection_id), reverse=True)
        stage = "signal"
        if lot_refs:
            stage = "traced"
        if matches:
            stage = "inspection"
        if any(item.outcome == "reproduced" for item in matches):
            stage = "reproduced"
        alert["case_key"] = content_hash([sku, alert["supplier"], alert["title"], issue])[:20]
        alert["quality_evidence"] = {
            "stage": stage, "inspections": [item.model_dump(mode="json") for item in matches],
        }
        for review in alert["evidence"]:
            review["trace_ref"] = traced.attribution.get(review["review_id"])
