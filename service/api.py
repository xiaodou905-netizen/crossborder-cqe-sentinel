"""Versioned operator endpoints for source runs and CQE follow-up."""

from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Request, Response

from .run_models import RunRequest, RunState, RunView, TriageRequest, TriageView
from .run_store import Conflict


router = APIRouter(prefix="/api/v1", tags=["Source workbench"])


@router.get("/workbench")
def status(request: Request) -> dict:
    runs = request.app.state.runs
    return {
        "public_demo": request.app.state.public_demo, "synthetic_sources": runs.synthetic,
        "latest": runs.store.latest(), "latest_ready": runs.store.latest(ready_only=True),
    }


@router.get("/contracts")
def contracts(request: Request) -> dict:
    return {adapter.source.value: adapter.page_model.model_json_schema()
            for adapter in request.app.state.runs.adapters}


@router.post("/runs", status_code=202)
async def start_run(
    request: Request, response: Response, body: RunRequest,
    key: Annotated[str | None, Header(alias="Idempotency-Key", min_length=1, max_length=80)] = None,
) -> RunView:
    runs = request.app.state.runs
    public_demo = request.app.state.public_demo
    if body.scenario != "normal" and (public_demo or not runs.synthetic):
        raise HTTPException(422, "故障回放仅用于本地合成上游。")
    if public_demo:
        existing = runs.store.latest()
        if existing is not None and existing.state != RunState.FAILED:
            response.headers["Location"] = f"/api/v1/runs/{existing.run_id}"
            return existing
        key = f"public-fixture-{existing.run_id}" if existing else "public-fixture-v1"
    try:
        run = runs.start(body.scenario, key)
    except Conflict as exc:
        raise HTTPException(409, str(exc)) from None
    response.headers["Location"] = f"/api/v1/runs/{run.run_id}"
    return run


@router.get("/runs/{run_id}")
def get_run(request: Request, run_id: str) -> RunView:
    run = request.app.state.runs.store.get(run_id)
    if run is None:
        raise HTTPException(404, "同步任务不存在。")
    return run


@router.get("/runs/{run_id}/report")
def get_snapshot(request: Request, run_id: str) -> dict:
    run = get_run(request, run_id)
    report = request.app.state.runs.store.report(run.run_id)
    if report is None:
        raise HTTPException(409, "本轮同步尚无可用快照。")
    return report


@router.post("/runs/{run_id}/activate")
def activate(request: Request, run_id: str) -> dict:
    return request.app.state.dashboard.show_sync(get_snapshot(request, run_id))


@router.post("/runs/{run_id}/cases/{case_key}/triage")
def triage(request: Request, run_id: str, case_key: str, body: TriageRequest) -> TriageView:
    if request.app.state.public_demo:
        raise HTTPException(403, "公共演示的研判记录为只读。")
    try:
        return request.app.state.runs.store.triage(run_id, case_key, body)
    except KeyError as exc:
        raise HTTPException(404, str(exc.args[0])) from None
    except Conflict as exc:
        raise HTTPException(409, str(exc)) from None
