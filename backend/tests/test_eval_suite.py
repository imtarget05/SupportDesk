"""Tests for the evaluation harness and its regression gate.

The harness is what CI trusts to catch a quality drop, so its own arithmetic
and its failure modes need to be pinned down.
"""

import importlib.util
import json
from pathlib import Path

import pytest

EVAL_DIR = Path(__file__).resolve().parents[2] / "evaluation"


def _load_suite():
    """Import eval_suite.py, which lives outside the backend package."""
    spec = importlib.util.spec_from_file_location(
        "eval_suite_under_test", EVAL_DIR / "eval_suite.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def suite():
    return _load_suite()


# ------------------------------------------------------------------ percentile


def test_percentile_of_empty_is_zero(suite):
    assert suite._percentile([], 95) == 0.0


def test_percentile_with_single_sample(suite):
    """Regression: an unclamped index returned None for a one-element list."""
    assert suite._percentile([7.5], 50) == 7.5
    assert suite._percentile([7.5], 95) == 7.5


def test_percentile_known_values(suite):
    values = [float(v) for v in range(1, 101)]
    assert suite._percentile(values, 50) == 50
    assert suite._percentile(values, 95) >= 95


def test_percentile_does_not_mutate_input(suite):
    values = [3.0, 1.0, 2.0]
    suite._percentile(values, 50)
    assert values == [3.0, 1.0, 2.0]


# ------------------------------------------------------------- regression gate


def _report(**overrides) -> dict:
    base = {
        "classification": {"accuracy": 0.9},
        "guardrails": {"block_rate": 0.1},
        "latency_ms": {"p95": 100.0},
    }
    for key, value in overrides.items():
        base[key] = value
    return base


def test_identical_reports_show_no_regression(suite):
    report = _report()
    assert suite.check_regression(report, report) == []


def test_accuracy_drop_is_flagged(suite):
    current = _report()
    baseline = _report(**{"classification": {"accuracy": 0.95}})
    problems = suite.check_regression(current, baseline)
    assert len(problems) == 1
    assert "accuracy dropped" in problems[0]


def test_small_accuracy_move_is_tolerated(suite):
    current = _report(**{"classification": {"accuracy": 0.94}})
    baseline = _report(**{"classification": {"accuracy": 0.95}})
    assert suite.check_regression(current, baseline) == []


def test_guardrail_rate_spike_is_flagged(suite):
    current = _report(**{"guardrails": {"block_rate": 0.5}})
    problems = suite.check_regression(current, _report())
    assert any("guardrail block rate rose" in p for p in problems)


def test_guardrail_rate_drop_is_not_a_regression(suite):
    """Fewer blocks is an improvement, not something to fail CI on."""
    current = _report(**{"guardrails": {"block_rate": 0.0}})
    assert suite.check_regression(current, _report()) == []


def test_latency_spike_is_flagged(suite):
    current = _report(**{"latency_ms": {"p95": 500.0}})
    problems = suite.check_regression(current, _report())
    assert any("p95 latency" in p for p in problems)


def test_zero_baseline_latency_does_not_divide_by_zero(suite):
    current = _report(**{"latency_ms": {"p95": 900.0}})
    assert suite.check_regression(current, _report(**{"latency_ms": {"p95": 0.0}})) == []


def test_multiple_regressions_are_all_reported(suite):
    current = _report(
        **{
            "classification": {"accuracy": 0.5},
            "guardrails": {"block_rate": 0.9},
            "latency_ms": {"p95": 999.0},
        }
    )
    assert len(suite.check_regression(current, _report())) == 3


# -------------------------------------------------------------- suite shape


def test_dataset_is_loaded(suite):
    data = suite.load_dataset()
    assert len(data) >= 90
    assert {"subject", "description", "expected_category"} <= set(data[0])


def test_run_suite_produces_every_metric_group(suite):
    report = suite.run_suite(limit=5)
    assert report["dataset_size"] == 5
    assert set(report) >= {
        "classification",
        "schema_validity",
        "guardrails",
        "cost",
        "latency_ms",
        "tokens",
    }
    assert 0.0 <= report["classification"]["accuracy"] <= 1.0
    assert 0.0 <= report["schema_validity"]["rate"] <= 1.0
    assert 0.0 <= report["guardrails"]["block_rate"] <= 1.0
    assert report["latency_ms"]["p50"] >= 0


def test_run_suite_is_deterministic(suite):
    """The offline stub must give the same answer twice, or the gate is noise."""
    first = suite.run_suite(limit=10)
    second = suite.run_suite(limit=10)
    assert first["classification"] == second["classification"]
    assert first["schema_validity"] == second["schema_validity"]


def test_unpriced_model_reports_gap_not_zero(suite):
    report = suite.run_suite(limit=3)
    assert report["cost"]["model_priced"] is False
    assert report["cost"]["usd_per_ticket"] is None


def test_render_handles_an_unpriced_model(suite):
    text = suite.render(suite.run_suite(limit=3))
    assert "not priced" in text
    assert "Schema validity" in text


def test_baseline_file_is_valid_json_with_the_documented_shape():
    path = EVAL_DIR / "baseline.json"
    if not path.exists():
        pytest.skip("baseline.json not generated yet")
    data = json.loads(path.read_text())
    assert {"classification", "guardrails", "latency_ms", "cost"} <= set(data)