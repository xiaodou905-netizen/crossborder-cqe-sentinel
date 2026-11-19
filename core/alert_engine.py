"""Compute review shares, rankings, and comparable-window alerts."""

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum
from fractions import Fraction

from .tagger import ISSUES, Category, EvidenceSpan, Issue, Tag, TaggedReview, evidence_spans


RECENT_DAYS = 7
BASELINE_DAYS = 28
MIN_RECENT_REVIEWS = 6
MIN_BASELINE_REVIEWS = 10
MIN_ISSUE_COUNT = 3
MIN_CHANGE_PP = 15
MIN_GROWTH = 2
HIGH_ISSUE_COUNT = 6
HIGH_CHANGE_PP = 30


class Level(str, Enum):
    HIGH = "重点预警"
    EARLY = "苗头关注"


@dataclass(frozen=True)
class Window:
    start: date
    end: date


@dataclass(frozen=True)
class WindowMetric:
    count: int
    sample: int
    share: float
    per_day: float


@dataclass(frozen=True)
class ReviewEvidence:
    review_id: str
    date: date
    lot: str
    stars: int
    review_text: str
    spans: list[EvidenceSpan]


@dataclass(frozen=True)
class Signal:
    sku: str
    supplier: str
    category: Category
    issue: Issue | None
    title: str
    level: Level
    recent: WindowMetric
    baseline: WindowMetric
    recent_rating_points: int
    change_pp: float
    lots: dict[str, int]
    keywords: list[str]
    evidence: list[ReviewEvidence]


def window_bounds(as_of: date) -> tuple[Window, Window]:
    recent_start = as_of - timedelta(days=RECENT_DAYS - 1)
    return (
        Window(recent_start, as_of),
        Window(recent_start - timedelta(days=BASELINE_DAYS), recent_start - timedelta(days=1)),
    )


def percentage(count: int, sample: int) -> float:
    return round(100 * count / sample, 1) if sample else 0.0


def issue_mix(rows: list[TaggedReview]) -> list[dict]:
    classified = Counter(row.tag.category for row in rows if row.tag.category is not None)
    sample = classified.total()
    return [
        {"category": category, "count": classified[category], "share": percentage(classified[category], sample)}
        for category in Category
    ]


def rank_groups(rows: list[TaggedReview], dimension: str) -> list[dict]:
    by_name = defaultdict(Counter)
    for row in rows:
        name = getattr(row.review, dimension)
        by_name[name][row.tag.category] += 1
    return [
        {
            "name": name,
            "count": counts.total(),
            "share": percentage(counts.total(), len(rows)),
            "by_category": {category: counts[category] for category in Category},
            "unclassified": counts[None],
        }
        for name, counts in sorted(by_name.items(), key=lambda pair: (-pair[1].total(), pair[0]))
    ]


def issue_key(tag: Tag) -> tuple[Category, Issue | None, str] | None:
    if tag.category is None:
        return None
    phrase = "" if tag.issue else " ".join(tag.keywords[0].casefold().split())
    return tag.category, tag.issue, phrase


def evaluate_signals(rows: list[TaggedReview], as_of: date) -> list[Signal]:
    recent_window, baseline_window = window_bounds(as_of)
    samples = defaultdict(lambda: ([], []))
    for item in rows:
        review = item.review
        group = (review.sku, review.supplier)
        if recent_window.start <= review.date <= recent_window.end:
            samples[group][0].append(item)
        elif baseline_window.start <= review.date <= baseline_window.end:
            samples[group][1].append(item)

    signals = []
    for (sku, supplier), (recent, baseline) in samples.items():
        if len(recent) < MIN_RECENT_REVIEWS or len(baseline) < MIN_BASELINE_REVIEWS:
            continue

        baseline_counts = Counter(issue_key(item.tag) for item in baseline)
        recent_by_issue = defaultdict(list)
        for item in recent:
            key = issue_key(item.tag)
            if key is not None:
                recent_by_issue[key].append(item)

        for key, matches in recent_by_issue.items():
            category, issue, phrase = key
            count = len(matches)
            prior_count = baseline_counts[key]
            share = Fraction(count, len(recent))
            prior_share = Fraction(prior_count, len(baseline))
            change_pp = (share - prior_share) * 100
            rate = Fraction(count, RECENT_DAYS)
            prior_rate = Fraction(prior_count, BASELINE_DAYS)
            if count < MIN_ISSUE_COUNT or change_pp < MIN_CHANGE_PP:
                continue
            if prior_count and (share < MIN_GROWTH * prior_share or rate < MIN_GROWTH * prior_rate):
                continue

            level = Level.HIGH if count >= HIGH_ISSUE_COUNT and change_pp >= HIGH_CHANGE_PP else Level.EARLY
            ordered = sorted(matches, key=lambda item: (item.review.date, item.review.review_id), reverse=True)
            keywords = Counter(term.casefold() for item in ordered for term in item.tag.keywords)
            signals.append(Signal(
                sku=sku,
                supplier=supplier,
                category=category,
                issue=issue,
                title=ISSUES[issue].name if issue else f"{category.value} · {phrase}",
                level=level,
                recent=WindowMetric(count, len(recent), percentage(count, len(recent)), round(float(rate), 2)),
                baseline=WindowMetric(prior_count, len(baseline), percentage(prior_count, len(baseline)), round(float(prior_rate), 2)),
                recent_rating_points=sum(item.review.stars for item in matches),
                change_pp=round(float(change_pp), 1),
                lots=dict(Counter(item.review.lot for item in ordered).most_common()),
                keywords=[term for term, _ in keywords.most_common(6)],
                evidence=[
                    ReviewEvidence(
                        item.review.review_id, item.review.date, item.review.lot, item.review.stars,
                        item.review.review_text, evidence_spans(item.review.review_text, item.tag.keywords),
                    )
                    for item in ordered[:3]
                ],
            ))

    return sorted(signals, key=lambda signal: (
        signal.level != Level.HIGH, -signal.change_pp, -signal.recent.count, signal.sku, signal.title,
    ))
