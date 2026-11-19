"""Persist synchronization runs and append-only CQE handoff decisions."""

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock

from adapters.contracts import Source
from adapters.http_feeds import Scenario, SourceStatus, canonical_json
from .run_models import RUN_TRANSITIONS, TERMINAL, RunState, RunView, TriageEvent, TriagePhase, TriageRequest, TriageView


class Conflict(ValueError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class RunStore:
    def __init__(self, path: Path | str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = RLock()
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE, state TEXT NOT NULL,
                view_json TEXT NOT NULL, report_json TEXT
            );
            CREATE TABLE IF NOT EXISTS triage_events (
                run_id TEXT REFERENCES runs(run_id), case_key TEXT NOT NULL,
                revision INTEGER NOT NULL, event_json TEXT NOT NULL,
                PRIMARY KEY (run_id, case_key, revision)
            );
        """)
        self._recover()

    def close(self) -> None:
        with self.lock:
            self.connection.close()

    def _recover(self) -> None:
        with self.lock:
            stale = self.connection.execute(
                "SELECT run_id FROM runs WHERE state IN ('queued', 'fetching', 'analyzing')"
            ).fetchall()
            for row in stale:
                self.update(row["run_id"], RunState.FAILED, error="服务重启中断了本轮同步，请创建新任务。")

    def create(self, key: str | None, scenario: Scenario, synthetic: bool) -> tuple[RunView, bool]:
        with self.lock, self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            if key is not None:
                existing = self.connection.execute("SELECT view_json FROM runs WHERE idempotency_key=?", (key,)).fetchone()
                if existing:
                    run = RunView.model_validate_json(existing["view_json"])
                    if run.scenario != scenario:
                        raise Conflict("该 Idempotency-Key 已用于不同的请求。")
                    return run, False
            active = self.connection.execute(
                "SELECT run_id FROM runs WHERE state IN ('queued', 'fetching', 'analyzing') LIMIT 1"
            ).fetchone()
            if active:
                raise Conflict("已有同步任务在运行，请先等待其结束。")
            now = utc_now()
            run = RunView(
                run_id=uuid.uuid4().hex, profile="synthetic_http" if synthetic else "configured_http",
                scenario=scenario, state=RunState.QUEUED, created_at=now, updated_at=now,
                sources=[SourceStatus(name, "pending") for name in Source],
            )
            self.connection.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?, NULL)",
                (run.run_id, key, run.state.value, run.model_dump_json()),
            )
            return run, True

    def get(self, run_id: str) -> RunView | None:
        with self.lock:
            row = self.connection.execute("SELECT view_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return RunView.model_validate_json(row["view_json"]) if row else None

    def latest(self, ready_only: bool = False) -> RunView | None:
        condition = " WHERE state='ready'" if ready_only else ""
        with self.lock:
            row = self.connection.execute(
                "SELECT view_json FROM runs" + condition + " ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        return RunView.model_validate_json(row["view_json"]) if row else None

    def update(
        self, run_id: str, state: RunState | None = None, *, source: SourceStatus | None = None,
        error: str | None = None, report: dict | None = None, wall_ms: int | None = None,
    ) -> None:
        with self.lock, self.connection:
            previous = self.get(run_id)
            if previous is None or previous.state in TERMINAL:
                raise RuntimeError("Cannot update an absent or terminal run")
            if state is not None and state not in RUN_TRANSITIONS[previous.state]:
                raise RuntimeError(f"Invalid run transition: {previous.state} → {state}")
            if (state is RunState.READY) != (report is not None):
                raise RuntimeError("A ready run and its report must be committed together")
            if state is not None:
                previous.state = state
            if source is not None:
                previous.sources = [source if item.source == source.source else item for item in previous.sources]
            if error:
                previous.error = error
            if state is RunState.FAILED:
                previous.sources = [
                    SourceStatus(item.source, "failed", error=error)
                    if item.status in {"pending", "running"} else item
                    for item in previous.sources
                ]
            if report is not None:
                previous.input_sha256 = report["sync"]["input_sha256"]
                previous.report_path = f"/api/v1/runs/{run_id}/report"
            if wall_ms is not None:
                previous.wall_ms = wall_ms
            previous.updated_at = utc_now()
            self.connection.execute(
                "UPDATE runs SET state=?, view_json=?, report_json=? WHERE run_id=?",
                (previous.state.value, previous.model_dump_json(), canonical_json(report) if report else None, run_id),
            )

    def report(self, run_id: str) -> dict | None:
        with self.lock:
            row = self.connection.execute("SELECT report_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if not row or not row["report_json"]:
                return None
            report = json.loads(row["report_json"])
            for alert in report["alerts"]:
                alert["triage"] = self._triage(run_id, alert["case_key"]).model_dump(mode="json")
            return report

    def _triage(self, run_id: str, case_key: str) -> TriageView:
        rows = self.connection.execute(
            "SELECT event_json FROM triage_events WHERE run_id=? AND case_key=? ORDER BY revision",
            (run_id, case_key),
        ).fetchall()
        events = [TriageEvent.model_validate_json(row["event_json"]) for row in rows]
        if events:
            return TriageView(phase=events[-1].to_phase, revision=events[-1].revision, history=events)
        return TriageView()

    def triage(self, run_id: str, case_key: str, change: TriageRequest) -> TriageView:
        with self.lock, self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            report = self.report(run_id)
            alert = next((item for item in report["alerts"] if item["case_key"] == case_key), None) if report else None
            if alert is None:
                raise KeyError("该同步快照内没有此质量事件。")
            current = TriageView.model_validate(alert["triage"])
            if current.revision != change.expected_revision:
                raise Conflict("研判版本已变化，请刷新后重试。")
            next_phase = {
                TriagePhase.PENDING: TriagePhase.INVESTIGATING,
                TriagePhase.INVESTIGATING: TriagePhase.VERIFICATION_REQUESTED,
            }.get(current.phase)
            if change.next_phase != next_phase:
                raise Conflict("当前研判状态不允许此流转。")
            if next_phase == TriagePhase.VERIFICATION_REQUESTED and alert["quality_evidence"]["stage"] != "reproduced":
                raise Conflict("提交复验需要同 SKU、批次和失效代码的复现记录。")
            event = TriageEvent(
                revision=current.revision + 1, from_phase=current.phase, to_phase=change.next_phase,
                note=change.note, recorded_at=utc_now(),
            )
            self.connection.execute(
                "INSERT INTO triage_events VALUES (?, ?, ?, ?)", (run_id, case_key, event.revision, event.model_dump_json())
            )
            return TriageView(phase=event.to_phase, revision=event.revision, history=[*current.history, event])
