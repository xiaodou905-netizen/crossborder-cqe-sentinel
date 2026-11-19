"""Exercise the real socket path against a running local synthetic workbench."""

import argparse
from time import monotonic, sleep

import httpx


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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8001")
    args = parser.parse_args()
    with httpx.Client(base_url=args.base, timeout=8, trust_env=False) as client:
        good = run(client, "normal")
        assert good["state"] == "ready", good
        report = client.get(good["report_path"]).json()
        assert report["input_rows"] == 408 and report["sync"]["traceability"]["linked"] == 77
        assert report["total_reviews"] == 80 and len(report["alerts"]) == 2
        assert report["alerts"][0]["quality_evidence"]["inspections"][0]["inspection_id"] == "QMS-26-117"
        before = client.get("/api/v1/workbench").json()
        if before["synthetic_sources"] and not before["public_demo"]:
            failed = run(client, "trace_unavailable")
            assert failed["state"] == "failed" and failed["sources"][1]["retries"] == 2, failed
            assert client.get("/api/v1/workbench").json()["latest_ready"]["run_id"] == good["run_id"]
            print("HTTP 503 × 3 → failed; last good snapshot preserved")
        for source in good["sources"]:
            print(f"{source['source']}: {source['rows']} rows, {source['pages']} pages, {source['elapsed_ms']} ms")
        print(f"80 low-star reviews, 77 traced, 2 alerts; snapshot {good['input_sha256'][:12]}")


if __name__ == "__main__":
    main()
