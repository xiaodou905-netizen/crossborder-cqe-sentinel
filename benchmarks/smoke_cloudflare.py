"""Check the unauthenticated public demo over HTTPS, including session isolation."""

import argparse
import hashlib
from time import monotonic, sleep
from urllib.parse import urlsplit

import httpx

from benchmarks.smoke_http import require


def check(client: httpx.Client, method: str, path: str, expected: int = 200, **kwargs) -> httpx.Response:
    response = client.request(method, path, **kwargs)
    require(response.status_code == expected, f"{method} {path}: expected {expected}, got {response.status_code}")
    return response


def completed(client: httpx.Client, location: str) -> dict:
    until = monotonic() + 32
    while monotonic() < until:
        state = check(client, "GET", location).json()
        if state["state"] in {"ready", "failed"}:
            return state
        sleep(.25)
    raise RuntimeError(f"Timed out polling {location}")


def run(client: httpx.Client, scenario: str, *, key: str) -> str:
    return check(
        client, "POST", "/api/v1/runs", 202, json={"scenario": scenario}, headers={"Idempotency-Key": key},
    ).headers["Location"]


def check_triage(client: httpx.Client, location: str, report: dict) -> None:
    alerts = {alert["issue"]: alert for alert in report["alerts"]}
    for issue, status in (("inner_tray_crush", 200), ("cold_fingerprint", 409)):
        path = f"{location}/cases/{alerts[issue]['case_key']}/triage"
        first = {"expected_revision": 0, "next_phase": "investigating", "note": "公网合成验收记录"}
        response = check(client, "POST", path, json=first).json()
        require(response["phase"] == "investigating", "Investigation transition did not persist")
        check(client, "POST", path, 409, json=first)
        response = check(
            client, "POST", path, status,
            json={"expected_revision": 1, "next_phase": "verification_requested", "note": "按公开合成样例复验"},
        ).json()
        if status == 200:
            require(response["revision"] == 2 and len(response["history"]) == 2, "Verification history is incomplete")


def smoke(base: str, proxy: str | None = None) -> None:
    with httpx.Client(base_url=base, timeout=12, trust_env=False, proxy=proxy) as client:
        home = check(client, "GET", "/")
        require("海外 VOC 质量预警" in home.text, "Dashboard HTML missing")
        if urlsplit(base).scheme == "https":
            cookie = home.headers.get("Set-Cookie", "")
            require("Secure" in cookie and "HttpOnly" in cookie, "Session cookie is not HTTPS protected")
        for path in ("/assets/dashboard.css", "/assets/dashboard.js", "/assets/workbench.js", "/assets/vendor/chart.umd.min.js"):
            require(len(check(client, "GET", path).content) > 100, f"Missing application asset: {path}")
        health = check(client, "GET", "/healthz").json()
        require(health["status"] == "ok" and health["sandbox"] == "cloudflare", "Wrong runtime")
        check(client, "GET", "/docs")
        require("paths" in check(client, "GET", "/openapi.json").json(), "OpenAPI missing")
        require(
            set(check(client, "GET", "/api/v1/contracts").json()) == {"reviews", "shipments", "inspections"},
            "Source schemas missing",
        )
        check(client, "GET", "/sources/historical_8d_kb.md")
        settings = check(client, "GET", "/api/v1/workbench").json()
        require(settings["public_demo"] and settings["synthetic_sources"] and settings["interactive_sandbox"], "Public sandbox mode is not enabled")
        require(check(client, "GET", "/api/report").json()["total_reviews"] == 80, "Initial report is incorrect")
        print("PASS homepage, assets, session cookie, health, OpenAPI, and synthetic defaults")

        page = check(client, "GET", "/demo-source/v1/reviews?limit=50").json()
        next_page = check(
            client, "GET", "/demo-source/v1/reviews",
            params={"limit": 50, "cursor": page["next_cursor"], "snapshot": page["snapshot_id"]},
        ).json()
        require(len(page["items"]) == len(next_page["items"]) == 50 and page["items"][0] != next_page["items"][0], "Source paging is incomplete")
        check(client, "GET", "/demo-source/v1/reviews?snapshot=stale", 409)
        check(client, "GET", "/demo-source/v1/shipments?fault=unavailable", 503)
        check(client, "GET", "/_sandbox/shipments-0.json", 404)

        location = run(client, "normal", key="success")
        require(run(client, "normal", key="success") == location, "Idempotency key created another run")
        ready = completed(client, location)
        require(ready["state"] == "ready", f"Sync failed: {ready.get('error')}")
        counts = [(item["rows"], item["pages"]) for item in ready["sources"]]
        require(counts == [(408, 9), (13, 1), (4, 1)], f"Unexpected source counts: {counts}")
        report = check(client, "GET", f"{location}/report").json()
        require(report["sync"]["traceability"]["linked"] == 77 and len(report["sync"]["trace_rejections"]) == 3, "Trace results changed")
        require(report["sync"]["input_sha256"] == ready["input_sha256"], "Snapshot hash does not match")
        require(check(client, "POST", f"{location}/activate").json()["data_source"] == "sync", "Snapshot activation failed")
        check_triage(client, location, report)
        snapshot = check(client, "GET", f"{location}/report").json()
        card = check(client, "GET", "/api/feishu-preview").json()
        require(card["simulated"] and "QMS-26-117" in str(card["payload"]), "Feishu preview lost its linked evidence")
        print("PASS feed pagination, successful sync, snapshot, trace evidence, CQE gates, and Feishu preview")

        failure_location = run(client, "trace_unavailable", key="fault")
        failed = completed(client, failure_location)
        require(failed["state"] == "failed" and failed["sources"][1]["retries"] == 2, "503 replay did not retry twice")
        check(client, "GET", f"{failure_location}/report", 409)
        require(check(client, "GET", f"{location}/report").json() == snapshot, "Failure changed the earlier snapshot")
        require(check(client, "GET", "/api/report").json()["sync"]["run_id"] == ready["run_id"], "Failure displaced active report")
        print("PASS 503 retry, failed-run status, and preserved good snapshot")

        check(client, "POST", "/api/restore-import", 404)
        sample = check(client, "GET", settings["sample_csv"]["url"]).content
        require(hashlib.sha256(sample).hexdigest() == settings["sample_csv"]["sha256"], "Downloaded CSV hash does not match")
        headers = {"Content-Type": "text/csv"}
        check(client, "POST", "/api/import", 422, content=b"review_id,rating\ntest,1\n", headers=headers)
        partial = check(client, "POST", "/api/import", content=sample, headers=headers).json()
        require(partial["rating_scope"] == "partial" and partial["input_rows"] == 408, "Partial CSV view is incorrect")
        full = check(client, "POST", "/api/import?full_ratings=true", content=sample, headers=headers).json()
        require(full["rating_scope"] == "full" and full["total_reviews"] == 80, "Full CSV view is incorrect")

        update = check(client, "POST", "/api/demo-simulate").json()
        require(update["total_reviews"] == 85 and update["alerts"][0]["recent"]["share"] == 76.5, "Simulation did not update counts and share")
        require(check(client, "POST", "/api/restore-import").json()["data_source"] == "import", "Import restoration failed")
        check(client, "POST", "/api/demo-simulate", 403, headers={"Origin": "https://not-the-sandbox.invalid"})
        with httpx.Client(base_url=base, timeout=12, trust_env=False, proxy=proxy) as second_session:
            check(second_session, "GET", "/")
            other_report = check(second_session, "GET", "/api/report").json()
            require(other_report["total_reviews"] == 80 and not other_report["has_import"], "Sessions share state")
            check(second_session, "GET", f"{location}/report", 404)
        print("PASS built-in CSV, restore, simulated growth, cross-origin rejection, and independent sessions")
        print(f"Verified Cloudflare sandbox {health['bundle']} at {base}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--proxy", help="Optional HTTP CONNECT proxy. TLS certificates are still verified.")
    args = parser.parse_args()
    target = urlsplit(args.base)
    local = target.hostname in {"127.0.0.1", "localhost"}
    if target.scheme != "https" and not (local and target.scheme == "http"):
        parser.error("Public verification requires HTTPS")
    try:
        smoke(args.base.rstrip("/"), args.proxy)
    except (httpx.HTTPError, RuntimeError, KeyError, ValueError) as exc:
        parser.exit(1, f"Cloudflare sandbox verification failed: {exc}\n")


if __name__ == "__main__":
    main()
