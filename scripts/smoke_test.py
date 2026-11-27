"""After a deploy, prove the live system works end to end: submit a ticket, wait for the worker.

    python scripts/smoke_test.py <api_url> <api_key>

Uses only the standard library, so it runs anywhere. It exits non-zero if the API rejects the
ticket, if the ticket isn't triaged within 3 minutes (cold start + queue + 3 LLM calls), or if a
fraud ticket is auto-resolved: the one outcome that must never ship.
"""

import json
import sys
import time
import urllib.request


def call(url: str, key: str, method: str = "GET", body: dict | None = None) -> dict:
    req = urllib.request.Request(
        url,
        method=method,
        data=json.dumps(body).encode() if body else None,
        headers={"x-api-key": key, "content-type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def main(base: str, key: str) -> None:
    base = base.rstrip("/")
    cases = {
        "routine": "How do I download my statement for March?",
        "fraud": "There are payments on my account to a casino I never made",
    }
    ids = {name: call(f"{base}/tickets", key, "POST", {"text": text})["ticket_id"]
           for name, text in cases.items()}  # fmt: skip
    deadline = time.time() + 180
    results: dict[str, dict] = {}
    while time.time() < deadline and len(results) < len(ids):
        time.sleep(5)
        for name, ticket_id in ids.items():
            item = call(f"{base}/tickets/{ticket_id}", key)
            if item["status"] != "queued":
                results[name] = item
    for name, item in results.items():
        r = item.get("result", {})
        print(f"{name}: {item['status']} ({r.get('category')}, {r.get('latency_ms')} ms)")
    if len(results) < len(ids):
        sys.exit("smoke test: tickets were not triaged within 3 minutes")
    if results["fraud"]["status"] == "auto_resolved":
        sys.exit("smoke test: a fraud ticket was auto-resolved")
    print("smoke test passed")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
