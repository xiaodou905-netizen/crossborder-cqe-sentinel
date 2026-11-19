"""Assemble a reproducible VOC and Listing report from a normalized sample."""

from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Literal

from fastapi.encoders import jsonable_encoder

from .alert_engine import evaluate_signals, issue_mix, rank_groups, window_bounds
from .listing_metrics import HORIZON_DAYS, RECOVERED_RATING, RECOVERY_SHARE, listing_metrics, load_demo_rating_buckets
from .rag_8d import CaseIndex, read_cases
from .review_data import load_reviews
from .tagger import Review, TaggedReview, tag_reviews


DATA = Path(__file__).resolve().parents[1] / "data"
REVIEWS = DATA / "amazon_reviews_mock.csv"
KNOWLEDGE_BASE = DATA / "historical_8d_kb.md"
DEMO_RATINGS = DATA / "demo_listing_ratings.csv"
RatingScope = Literal["demo", "full", "partial"]
DataSource = Literal["demo", "import", "sync"]


@dataclass(frozen=True)
class ReviewSample:
    reviews: list[Review]
    tagged: list[TaggedReview]
    as_of: date
    rating_scope: RatingScope
    source: DataSource
    simulated_reviews: int = 0


class Analyzer:
    def __init__(self, demo_mode: bool):
        self.demo_mode = demo_mode
        self.cases = CaseIndex(read_cases(KNOWLEDGE_BASE))
        self.demo_ratings = load_demo_rating_buckets(DEMO_RATINGS)

    def prepare(
        self, reviews: list[Review], scope: RatingScope, source: DataSource, as_of: date | None = None
    ) -> ReviewSample:
        cutoff = as_of or max((row.date for row in reviews), default=date.today())
        window_bounds(cutoff)
        included = [row for row in reviews if row.date <= cutoff]
        tagged = tag_reviews([row for row in included if row.stars <= 2], self.demo_mode)
        return ReviewSample(included, tagged, cutoff, scope, source)

    def prepare_demo(self, as_of: date | None) -> ReviewSample:
        return self.prepare(load_reviews(REVIEWS), "demo", "demo", as_of)

    def report(self, sample: ReviewSample) -> dict:
        recent, baseline = window_bounds(sample.as_of)
        matches = [
            (signal, self.cases.recommend(signal, sample.as_of))
            for signal in evaluate_signals(sample.tagged, sample.as_of)
        ]
        is_demo = sample.source == "demo"
        return jsonable_encoder({
            "demo_mode": self.demo_mode,
            "data_source": sample.source,
            "rating_scope": sample.rating_scope,
            "has_import": sample.source == "import",
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
                sample.reviews, self.demo_ratings if is_demo else (), recent,
                [signal for signal, match in matches if match is not None], full_ratings=sample.rating_scope != "partial",
            ),
            "listing_scenario": {
                "days": HORIZON_DAYS, "recovery_share": RECOVERY_SHARE, "recovered_rating": RECOVERED_RATING,
            },
        })
