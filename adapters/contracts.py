"""Versioned exchange schemas at the boundary of the workbench."""

from datetime import date
from enum import Enum
from typing import Annotated, Generic, Literal, Self, TypeVar

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from core.tagger import Identifier, Issue


class Source(str, Enum):
    REVIEWS = "reviews"
    SHIPMENTS = "shipments"
    INSPECTIONS = "inspections"


class FeedRow(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class MarketplaceReview(FeedRow):
    review_id: Identifier
    listing_sku: Identifier
    posted_on: date
    rating: int = Field(ge=1, le=5)
    body: str = Field(max_length=16_000)
    brand: Identifier
    trace_ref: Identifier | None


class ShipmentTrace(FeedRow):
    trace_ref: Identifier
    sku: Identifier
    lot: Identifier
    supplier: Identifier
    released_on: date


class Inspection(FeedRow):
    inspection_id: Identifier
    sku: Identifier
    lot: Identifier
    inspected_on: date
    issue: Issue
    outcome: Literal["observed", "reproduced", "fix_verified"]
    sample_size: int = Field(gt=0, le=100_000)
    failures: int = Field(ge=0)
    method: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=400)]

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.failures > self.sample_size:
            raise ValueError("failures exceed sample size")
        if self.outcome == "reproduced" and not self.failures:
            raise ValueError("reproduced requires a failure")
        if self.outcome == "fix_verified" and self.failures:
            raise ValueError("fix_verified requires zero observed failures")
        return self


Row = TypeVar("Row", bound=FeedRow)
Coverage = Literal["all_ratings", "partial_ratings", "records"]
Cursor = Annotated[str, StringConstraints(min_length=1, max_length=160)]


class FeedPage(FeedRow, Generic[Row]):
    schema_version: Literal["1.0"]
    snapshot_id: Identifier
    coverage: Coverage
    items: list[Row] = Field(max_length=100)
    next_cursor: Cursor | None
