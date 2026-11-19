"""Service API types and transition conditions."""

from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from adapters.http_feeds import Scenario, SourceStatus


class RunState(str, Enum):
    QUEUED = "queued"
    FETCHING = "fetching"
    ANALYZING = "analyzing"
    READY = "ready"
    FAILED = "failed"


RUN_TRANSITIONS = {
    RunState.QUEUED: {RunState.FETCHING, RunState.FAILED},
    RunState.FETCHING: {RunState.ANALYZING, RunState.FAILED},
    RunState.ANALYZING: {RunState.READY, RunState.FAILED},
    RunState.READY: set(),
    RunState.FAILED: set(),
}
TERMINAL = {RunState.READY, RunState.FAILED}


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario: Scenario = "normal"


class RunView(BaseModel):
    run_id: str
    scenario: Scenario
    profile: Literal["synthetic_http", "configured_http"]
    state: RunState
    created_at: str
    updated_at: str
    sources: list[SourceStatus]
    error: str | None = None
    input_sha256: str | None = None
    report_path: str | None = None
    wall_ms: int | None = None


class TriagePhase(str, Enum):
    PENDING = "pending"
    INVESTIGATING = "investigating"
    VERIFICATION_REQUESTED = "verification_requested"


class TriageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(strict=True, ge=0)
    next_phase: TriagePhase
    note: Annotated[str, StringConstraints(strip_whitespace=True, min_length=4, max_length=500)]


class TriageEvent(BaseModel):
    revision: int
    from_phase: TriagePhase
    to_phase: TriagePhase
    note: str
    recorded_at: str


class TriageView(BaseModel):
    phase: TriagePhase = TriagePhase.PENDING
    revision: int = 0
    history: list[TriageEvent] = Field(default_factory=list)
