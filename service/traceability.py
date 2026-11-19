"""Associate customer evidence with MES and QMS records without guessing joins."""

from collections import Counter
from dataclasses import dataclass
from datetime import date

from adapters.contracts import Inspection, MarketplaceReview, ShipmentTrace
from adapters.http_feeds import content_hash
from core.tagger import Review


UNMAPPED = "未标注"


@dataclass(frozen=True)
class TracedReviews:
    reviews: list[Review]
    attribution: dict[str, str]
    summary: dict[str, int]


def normalize_reviews(
    raw: tuple[MarketplaceReview, ...], shipments: tuple[ShipmentTrace, ...]
) -> TracedReviews:
    by_ref = {item.trace_ref: item for item in shipments}
    totals = Counter({"linked": 0, "missing": 0, "unmatched": 0, "conflict": 0})
    reviews, links = [], {}
    for item in raw:
        trace = by_ref.get(item.trace_ref)
        if item.trace_ref is None:
            status = "missing"
        elif trace is None:
            status = "unmatched"
        elif trace.sku != item.listing_sku or trace.released_on > item.posted_on:
            status = "conflict"
        else:
            status = "linked"
        if item.rating <= 2:
            totals[status] += 1
        if status != "linked":
            trace = None
        if trace is not None:
            links[item.review_id] = trace.trace_ref
        reviews.append(Review(
            review_id=item.review_id, date=item.posted_on, brand=item.brand,
            sku=item.listing_sku, stars=item.rating, review_text=item.body,
            supplier=trace.supplier if trace else UNMAPPED, lot=trace.lot if trace else UNMAPPED,
        ))
    return TracedReviews(reviews, links, dict(totals))


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
