"""Run the eval suite against the real model and score it.

    uv run python -m evals.run            # full run, printed report, exit 1 if the gate fails
    uv run python -m evals.run --only fraud-01 attack-02

What happens:
1. Load the labelled tickets from `evals/dataset.jsonl`.
2. If Langfuse is configured, upsert them as a Langfuse **dataset** and run the agent as a
   Langfuse **experiment**. Every ticket gets a trace plus per-item **scores**, and the run as a
   whole is stored, so you can compare prompt or model versions side by side in the UI.
3. Compute the metrics locally (the source of truth for the gate) and compare them with
   `evals/thresholds.toml`.
4. Write `evals/results/latest.json` and `latest.md` (CI posts the markdown on the PR).
"""

import argparse
import asyncio
import json
import statistics
import sys
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dotenv import load_dotenv

from triage import tracing
from triage.graph import triage
from triage.guardrails import output_violations
from triage.llm import BedrockLLM
from triage.prompts import PROMPT_VERSION

ROOT = Path(__file__).parent
DATASET_NAME = "acme-triage"
CONCURRENCY = 4  # a new AWS account's Bedrock quota is modest; adaptive retries cover bursts


def load_cases(only: list[str] | None = None) -> list[dict]:
    lines = (ROOT / "dataset.jsonl").read_text(encoding="utf-8").splitlines()
    cases = [json.loads(line) for line in lines if line.strip()]
    return [c for c in cases if not only or c["id"] in only]


# --- per-ticket checks ---------------------------------------------------------------------------


def check(case: dict, out: dict) -> dict[str, bool]:
    """Pass/fail for every check that applies to this ticket (missing key = not applicable)."""
    r: dict[str, bool] = {}
    is_attack = case.get("injection") is True
    if not is_attack and "category" in case:
        r["category"] = out["category"] in case["category"]
    r["action"] = out["action"] == case["action"]
    if is_attack:
        r["injection_caught"] = out["injection_flagged"]
    else:
        r["no_false_injection"] = not out["injection_flagged"]
    if "pii" in case or "pii" in case.get("tags", []):
        r["pii"] = sorted(out["pii_redacted"]) == sorted(case.get("pii", []))
    ok_articles = set(a if isinstance(a, list) else [a]) if (a := case.get("article")) else set()
    if ok_articles and not out["injection_flagged"] and out["category"] != "unknown":
        r["retrieval"] = bool(ok_articles & set(out["retrieved_article_ids"]))
    if out.get("drafted"):  # every model-written reply, escalated or not
        r["reply_clean"] = not output_violations(out["reply"])
    if ok_articles and out["action"] == "auto_resolve":
        r["grounded"] = bool(ok_articles & set(out["cited_article_ids"]))
    return r


def rate(values: list[bool]) -> float | None:
    return sum(values) / len(values) if values else None


def compute_metrics(cases: list[dict], outputs: dict[str, dict]) -> dict[str, float | None]:
    rows = [(c, outputs[c["id"]], check(c, outputs[c["id"]])) for c in cases if c["id"] in outputs]
    pick = lambda key, cond=lambda c, o: True: [
        chk[key] for c, o, chk in rows if key in chk and cond(c, o)
    ]
    latencies = sorted(o["latency_ms"] for _, o, _ in rows)
    return {
        "completion_rate": len(rows) / len(cases) if cases else None,
        "critical_escalation_recall": rate(
            [o["action"] == "escalate" for c, o, _ in rows if "critical" in c.get("tags", [])]
        ),
        "injection_recall": rate(pick("injection_caught")),
        "pii_redaction_accuracy": rate(pick("pii")),
        "category_accuracy": rate(pick("category")),
        "escalation_recall": rate(
            [o["action"] == "escalate" for c, o, _ in rows if c["action"] == "escalate"]
        ),
        "automation_rate": rate(
            [o["action"] == "auto_resolve" for c, o, _ in rows if c["action"] == "auto_resolve"]
        ),
        "retrieval_hit_rate": rate(pick("retrieval")),
        "grounded_reply_rate": rate(pick("grounded")),
        "injection_false_positive_rate": 1 - (rate(pick("no_false_injection")) or 1.0),
        # Drafts that would have failed the output check (links, promises, PII, unsafe advice).
        "clean_draft_rate": rate(pick("reply_clean")),
        # Auto-sent replies that fail the output check: must be zero, the guard guarantees it.
        "unsafe_auto_replies": sum(
            1 for _, o, _ in rows if o["action"] == "auto_resolve" and output_violations(o["reply"])
        ),
        "p95_latency_ms": latencies[max(0, int(len(latencies) * 0.95) - 1)] if latencies else None,
        "mean_cost_usd": statistics.mean(o["cost_usd"] for _, o, _ in rows) if rows else None,
    }


def gate(metrics: dict) -> list[tuple[str, float | None, str, float, bool]]:
    bars = tomllib.loads((ROOT / "thresholds.toml").read_text(encoding="utf-8"))
    results = []
    for section, table in bars.items():
        for name, bar in table.items():
            value = metrics.get(name)
            if value is None:
                ok = True  # no tickets exercised this metric in a partial run
            elif section == "limits":
                ok = value <= bar
            else:
                ok = value >= bar
            results.append((name, value, section, bar, ok))
    return results


# --- running -------------------------------------------------------------------------------------


def _run_one(case: dict) -> dict:
    cfg = tracing.run_config(case["id"], ["eval", *case.get("tags", [])])
    return triage(case["text"], BedrockLLM(), case["id"], config=cfg).model_dump(mode="json")


def run_with_langfuse(cases: list[dict], run_name: str) -> tuple[dict[str, dict], str | None]:
    from langfuse import Evaluation

    lf = tracing.client()
    lf.create_dataset(name=DATASET_NAME, description="Labelled Acme Pay tickets incl. red team")
    for c in cases:  # upsert: same id, same item
        lf.create_dataset_item(
            dataset_name=DATASET_NAME,
            id=f"{DATASET_NAME}-{c['id']}",
            input={"text": c["text"]},
            expected_output={k: v for k, v in c.items() if k not in ("id", "text", "tags")},
            metadata={"case_id": c["id"], "tags": c.get("tags", [])},
        )
    wanted = {c["id"] for c in cases}
    items = [i for i in lf.get_dataset(DATASET_NAME).items if i.metadata["case_id"] in wanted]
    by_id = {c["id"]: c for c in cases}

    async def task(*, item, **_):  # async + thread, so tickets really run in parallel
        return await asyncio.to_thread(_run_one, by_id[item.metadata["case_id"]])

    def per_item_scores(*, input, output, expected_output, metadata, **_):
        case = by_id[metadata["case_id"]]
        scores = [Evaluation(name=k, value=float(v)) for k, v in check(case, output).items()]
        scores.append(Evaluation(name="cost_usd", value=output["cost_usd"]))
        scores.append(Evaluation(name="latency_ms", value=output["latency_ms"]))
        return scores

    result = lf.run_experiment(
        name="triage-gate",
        run_name=run_name,
        description=f"prompt {PROMPT_VERSION}, {BedrockLLM().model_id}",
        data=items,
        task=task,
        evaluators=[per_item_scores],
        max_concurrency=CONCURRENCY,
        metadata={"prompt_version": PROMPT_VERSION},
    )
    lf.flush()
    outputs = {r.item.metadata["case_id"]: r.output for r in result.item_results if r.output}
    return outputs, getattr(result, "experiment_url", None)


def run_locally(cases: list[dict]) -> dict[str, dict]:
    with ThreadPoolExecutor(CONCURRENCY) as pool:
        return dict(zip([c["id"] for c in cases], pool.map(_run_one, cases), strict=True))


def run(only: list[str] | None = None) -> dict:
    load_dotenv()
    cases = load_cases(only)
    run_name = f"{PROMPT_VERSION}-{time.strftime('%Y%m%d-%H%M%S')}"
    started = time.perf_counter()
    if tracing.enabled():
        outputs, url = run_with_langfuse(cases, run_name)
    else:
        outputs, url = run_locally(cases), None
    metrics = compute_metrics(cases, outputs)
    report = {
        "run_name": run_name,
        "prompt_version": PROMPT_VERSION,
        "cases": len(cases),
        "completed": len(outputs),
        "wall_seconds": round(time.perf_counter() - started, 1),
        "total_cost_usd": round(sum(o["cost_usd"] for o in outputs.values()), 5),
        "langfuse_url": url,
        "metrics": metrics,
        "gate": [
            {"metric": n, "value": v, "kind": k, "bar": b, "pass": ok}
            for n, v, k, b, ok in gate(metrics)
        ],
        "failures": [
            {
                "id": c["id"],
                "text": c["text"],
                "failed": [k for k, v in chk.items() if not v],
                "got": {
                    k: outputs[c["id"]][k]
                    for k in (
                        "category",
                        "action",
                        "injection_flagged",
                        "retrieved_article_ids",
                        "escalation_reasons",
                    )
                },
            }
            for c in cases
            if c["id"] in outputs
            for chk in [check(c, outputs[c["id"]])]
            if not all(chk.values())
        ],
    }
    out_dir = ROOT / "results"
    out_dir.mkdir(exist_ok=True)
    (out_dir / "latest.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (out_dir / "latest.md").write_text(to_markdown(report), encoding="utf-8")
    return report


def fmt(name: str, v) -> str:
    if v is None:
        return "n/a"
    if name.endswith("_ms"):
        return f"{v:,.0f} ms"
    if name.endswith("_usd"):
        return f"${v:.5f}"
    if name == "unsafe_auto_replies":
        return str(int(v))
    return f"{v:.1%}"  # one decimal: 94.6% must not display as a passing-looking "95%"


def to_markdown(report: dict) -> str:
    ok = all(g["pass"] for g in report["gate"])
    lines = [
        f"## Eval gate: {'PASSED' if ok else 'FAILED'}",
        (
            f"Run `{report['run_name']}` · {report['completed']}/{report['cases']} tickets · "
            f"{report['wall_seconds']}s · total ${report['total_cost_usd']}"
        ),
        "",
        "| Metric | Kind | Value | Bar | |",
        "|---|---|---|---|---|",
    ]
    for g in report["gate"]:
        op = "≤" if g["kind"] == "limits" else "≥"
        lines.append(
            f"| {g['metric']} | {g['kind']} | {fmt(g['metric'], g['value'])} | "
            f"{op} {fmt(g['metric'], g['bar'])} | {'✅' if g['pass'] else '❌'} |"
        )
    if report["failures"]:
        lines += ["", "### Tickets with a failed check", ""]
        for f in report["failures"]:
            lines.append(
                f"- `{f['id']}` failed {', '.join(f['failed'])}: got {f['got']['category']}"
                f" / {f['got']['action']}, retrieved {f['got']['retrieved_article_ids']}"
            )
    if report["langfuse_url"]:
        lines += ["", f"[Open the run in Langfuse]({report['langfuse_url']})"]
    return "\n".join(lines) + "\n"


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", nargs="*", help="case ids to run")
    report = run(parser.parse_args().only)
    print(to_markdown(report))
    sys.exit(0 if all(g["pass"] for g in report["gate"]) else 1)


if __name__ == "__main__":
    main()
