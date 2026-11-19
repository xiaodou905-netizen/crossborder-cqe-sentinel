"""Verify the synthetic workbench through real loopback HTTP."""

import argparse
import os
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from threading import Thread
from time import monotonic, sleep
from unittest.mock import patch

import httpx
import uvicorn

from adapters.contracts import Source


@contextmanager
def standalone_base() -> Iterator[str]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        sandbox_env = {
            "PORT": str(port), "DEMO_MODE": "1", "SENTINEL_PUBLIC_DEMO": "0", "SENTINEL_DB_PATH": ":memory:",
            **{f"SENTINEL_{source.value.upper()}_URL": "" for source in Source},
        }
        with patch.dict(os.environ, sandbox_env):
            from app import create_app

            config = uvicorn.Config(create_app(), log_level="warning", access_log=False)
            server = uvicorn.Server(config)
            worker = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
            worker.start()
            try:
                deadline = monotonic() + 10
                while not server.started:
                    if not worker.is_alive() or monotonic() >= deadline:
                        raise RuntimeError("本地验收沙盒未能启动。")
                    sleep(.05)
                yield f"http://127.0.0.1:{port}"
            finally:
                server.should_exit = True
                worker.join(timeout=8)
                if worker.is_alive():
                    raise RuntimeError("本地验收沙盒未能按时退出。")


def run(client: httpx.Client, scenario: str) -> dict:
    accepted = client.post("/api/v1/runs", json={"scenario": scenario})
    accepted.raise_for_status()
    deadline = monotonic() + 35
    while monotonic() < deadline:
        response = client.get(accepted.headers["location"])
        response.raise_for_status()
        job = response.json()
        if job["state"] in {"ready", "failed"}:
            return job
        sleep(.1)
    raise RuntimeError("The sync job did not finish")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def verify(base: str) -> None:
    with httpx.Client(base_url=base, timeout=8, trust_env=False) as client:
        good = run(client, "normal")
        require(good["state"] == "ready", f"三源回放未完成（{good['state']}）")
        snapshot = client.get(good["report_path"])
        snapshot.raise_for_status()
        report = snapshot.json()
        require(report["input_rows"] == 408 and report["total_reviews"] == 80, "评分或差评分母变化")
        sync = report["sync"]
        require(sync["traceability"] == {"linked": 77, "missing": 1, "unmatched": 1, "conflict": 1}, "追溯统计变化")
        require(
            {item["reason"] for item in sync["trace_rejections"]} == {"missing_reference", "unknown_reference", "sku_mismatch"},
            "未关联差评原因变化",
        )
        alerts = {alert["issue"]: alert["quality_evidence"] for alert in report["alerts"]}
        require(set(alerts) == {"inner_tray_crush", "cold_fingerprint"}, "预埋的两条告警变化")

        for issue, stage, qms_id in (
            ("inner_tray_crush", "reproduced", "QMS-26-117"),
            ("cold_fingerprint", "inspection", "QMS-26-123"),
        ):
            evidence = alerts[issue]
            require(
                evidence["stage"] == stage and [item["inspection_id"] for item in evidence["inspections"]] == [qms_id],
                f"{issue} 的 QMS 记录或证据阶段变化",
            )
            record, = evidence["inspections"]
            print(f"{issue}: {record['lot']} · {qms_id} · {record['failures']}/{record['sample_size']} 异常 · {stage}")

        before = client.get("/api/v1/workbench").json()
        if before["synthetic_sources"] and not before["public_demo"]:
            failed = run(client, "trace_unavailable")
            require(failed["state"] == "failed" and failed["sources"][1]["retries"] == 2, "追溯故障回放结果变化")
            latest_ready = client.get("/api/v1/workbench").json()["latest_ready"]
            require(latest_ready["run_id"] == good["run_id"], "上一次成功快照被失败任务替换")
            require(client.get(good["report_path"]).json() == report, "故障后回读的成功快照发生变化")
            print("HTTP 503 × 3 → failed; last good snapshot preserved")
        for source in good["sources"]:
            print(f"{source['source']}: {source['rows']} rows, {source['pages']} pages, {source['elapsed_ms']} ms")
        print(f"80 low-star reviews, 77 traced, 2 alerts; snapshot {good['input_sha256'][:12]}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--base", default="http://127.0.0.1:8001")
    target.add_argument("--standalone", action="store_true", help="Start an isolated built-in sandbox, verify, and exit")
    args = parser.parse_args()
    try:
        if args.standalone:
            with standalone_base() as base:
                verify(base)
        else:
            verify(args.base)
    except (httpx.HTTPError, RuntimeError, OSError) as exc:
        parser.exit(status=1, message=f"演示验收失败：{exc}\n")


if __name__ == "__main__":
    main()
