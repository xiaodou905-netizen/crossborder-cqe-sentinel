"""Select the active view while keeping uploads and demo samples independent."""

from dataclasses import replace
from datetime import date
from threading import Lock

from core.reporting import Analyzer
from core.review_data import simulated_packaging
from core.tagger import Review, tag_reviews


class Workbench:
    def __init__(self, as_of: date | None, analyzer: Analyzer):
        self._lock = Lock()
        self.analyzer = analyzer
        self._demo_sample = analyzer.prepare_demo(as_of)
        self._imported_report: dict | None = None
        self.report = analyzer.report(self._demo_sample)

    def import_reviews(self, reviews: list[Review], full_ratings: bool) -> dict:
        sample = self.analyzer.prepare(reviews, "full" if full_ratings else "partial", "import")
        report = self.analyzer.report(sample)
        with self._lock:
            self._imported_report = self.report = report
        return report

    def simulate(self) -> dict:
        with self._lock:
            demo = self._demo_sample
            new_reviews = simulated_packaging(demo.as_of, demo.simulated_reviews)
            sample = replace(
                demo, reviews=demo.reviews + new_reviews,
                tagged=demo.tagged + tag_reviews(new_reviews, demo_mode=True),
                simulated_reviews=demo.simulated_reviews + len(new_reviews),
            )
            report = self.analyzer.report(sample)
            report["has_import"] = self._imported_report is not None
            self._demo_sample = sample
            self.report = report
            return report

    def restore_import(self) -> dict | None:
        with self._lock:
            if self._imported_report is not None:
                self.report = self._imported_report
            return self._imported_report

    def show_sync(self, report: dict) -> dict:
        with self._lock:
            self.report = {**report, "has_import": self._imported_report is not None}
            return self.report
