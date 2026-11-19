"""Estimate sample ratings and the 30-day effect of preventing targeted complaints."""

import csv
from collections import Counter
from dataclasses import dataclass
from datetime import date
from itertools import chain
from pathlib import Path

from .alert_engine import RECENT_DAYS, Signal, Window
from .tagger import Review


HORIZON_DAYS = 30
RECOVERY_SHARE = 0.7
RECOVERED_RATING = 5


@dataclass(frozen=True)
class RatingBucket:
    date: date
    sku: str
    stars: int
    count: int


def load_demo_rating_buckets(path: Path) -> tuple[RatingBucket, ...]:
    with path.open(encoding="utf-8-sig", newline="") as source:
        return tuple(
            RatingBucket(date.fromisoformat(row["date"]), row["sku"], int(row["stars"]), int(row["count"]))
            for row in csv.DictReader(source)
        )


def listing_metrics(
    reviews: list[Review], extra_ratings: tuple[RatingBucket, ...], window: Window,
    linked_signals: list[Signal], full_ratings: bool,
) -> list[dict]:
    ratings = Counter()
    points = Counter()
    recent_ratings = Counter()
    recent_points = Counter()
    negatives = Counter(row.sku for row in reviews if row.stars <= 2)

    individual = (RatingBucket(row.date, row.sku, row.stars, 1) for row in reviews)
    for bucket in chain(individual, extra_ratings):
        if bucket.date > window.end:
            continue
        ratings[bucket.sku] += bucket.count
        points[bucket.sku] += bucket.stars * bucket.count
        if bucket.date >= window.start:
            recent_ratings[bucket.sku] += bucket.count
            recent_points[bucket.sku] += bucket.stars * bucket.count

    affected = Counter()
    recoverable_points = Counter()
    for signal in linked_signals:
        affected[signal.sku] += signal.recent.count
        recoverable_points[signal.sku] += RECOVERED_RATING * signal.recent.count - signal.recent_rating_points

    metrics = []
    multiplier = HORIZON_DAYS / RECENT_DAYS
    for sku in sorted(ratings, key=lambda sku: (-negatives[sku], sku)):
        current_stars = round(points[sku] / ratings[sku], 3) if full_ratings else None
        no_action = after_8d = lift = None
        if full_ratings and affected[sku] and recent_ratings[sku]:
            projected_count = ratings[sku] + recent_ratings[sku] * multiplier
            projected_points = points[sku] + recent_points[sku] * multiplier
            improvement = recoverable_points[sku] * multiplier * RECOVERY_SHARE
            no_action = round(projected_points / projected_count, 3)
            after_8d = round((projected_points + improvement) / projected_count, 3)
            lift = round(improvement / projected_count, 3)

        metrics.append({
            "sku": sku, "rating_count": ratings[sku], "recent_rating_count": recent_ratings[sku],
            "current_stars": current_stars, "without_8d_stars": no_action,
            "with_8d_stars": after_8d, "lift_stars": lift, "linked_recent_reviews": affected[sku],
        })
    return metrics
