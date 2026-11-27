"""The eval suite as pytest tests, one test per gate metric, so CI shows exactly which bar failed.

    uv run pytest evals -v        # calls the real model: needs AWS credentials, costs ~1p

The suite runs once per session (fixture) and each test reads its metric from the report.
"""

import tomllib
from pathlib import Path

import pytest

from evals.run import run

BARS = tomllib.loads((Path(__file__).parent / "thresholds.toml").read_text(encoding="utf-8"))
METRICS = [(kind, name, bar) for kind, table in BARS.items() for name, bar in table.items()]


@pytest.fixture(scope="session")
def report():
    return run()


@pytest.mark.parametrize(("kind", "name", "bar"), METRICS, ids=[m[1] for m in METRICS])
def test_gate(report, kind, name, bar):
    value = report["metrics"][name]
    assert value is not None, f"no tickets exercised {name}"
    failures = [f["id"] for f in report["failures"]]
    if kind == "limits":
        assert value <= bar, f"{name}={value} exceeds {bar}"
    else:
        assert value >= bar, f"{name}={value:.2%} below {bar:.0%}; failing tickets: {failures}"
