"""Evaluation harness for the AI features, not just the classifier.

`evaluate.py` answers "how accurate is the triage?". This answers the question
an engineer actually has before shipping an AI feature: how often does it fail
in each distinct way, what does it cost, and how slow is it.

Four metric groups, all measured on the labeled dataset:

  classification   accuracy / macro-F1 / per-category F1 (same math as
                   evaluate.py, so the numbers are directly comparable)
  schema_validity  share of calls whose output parses into the expected schema.
                   A model that answers every ticket wrongly but always validly
                   is a different problem from one that returns garbage.
  guardrails       how often the deterministic guardrails block a draft. A jump
                   means the model drifted toward unsafe phrasing.
  cost / latency   USD and milliseconds per ticket, from real usage counters,
                   with p50 and p95.

Offline by default: `AI_PROVIDER=stub python evaluation/eval_suite.py`.
`--check` compares against `evaluation/baseline.json` and exits non-zero on
regression — that is what the CI gate runs.

Pure stdlib apart from the app itself: no benchmark framework, because the
point is to measure this system, not to publish a leaderboard.
"""

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

EVAL = Path(__file__).resolve().parent
DATASET = EVAL / "tickets.json"
BASELINE = EVAL / "baseline.json"

# Regressions beyond these bounds fail `--check`. Accuracy is in percentage
# points; the guardrail rate is an absolute change.
MAX_ACCURACY_DROP_PP = 2.0
MAX_GUARDRAIL_RATE_RISE = 0.10
MAX_P95_LATENCY_RATIO = 2.0


def load_dataset() -> list[dict]:
    return json.loads(DATASET.read_text())


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    # Clamp to the first element: with a single sample every percentile is that
    # sample, and an unclamped index can go negative.
    index = min(
        len(ordered) - 1, max(0, int(round((pct / 100) * len(ordered) + 0.5)) - 1)
    )
    return ordered[index]


def run_suite(limit: int | None = None) -> dict:
    """Run the dataset through the active provider and collect every metric."""
    from app.services import ai_service, metrics, pricing

    data = load_dataset()
    if limit:
        data = data[:limit]

    categories = sorted({row["expected_category"] for row in data})
    tp: dict[str, int] = {c: 0 for c in categories}
    fp: dict[str, int] = {}
    fn: dict[str, int] = {c: 0 for c in categories}
    provider_errors = 0

    schema_ok = 0
    guardrail_blocks = 0
    drafts_checked = 0
    latencies: list[float] = []
    per_call_costs: list[float] = []
    total_tokens = 0

    model = str(getattr(ai_service.get_provider(), "model", "") or "")

    for row in data:
        started = time.perf_counter()
        metrics.reset()
        try:
            result = ai_service.analyze_ticket(row["subject"], row["description"])
        except ai_service.AIProviderError:
            # Nothing usable came back, so this also counts as a schema failure.
            provider_errors += 1
            continue
        finally:
            latencies.append((time.perf_counter() - started) * 1000)

        # Reaching this point with a validated AnalysisResult *is* the schema
        # check: the provider's raw output had to parse into it.
        schema_ok += 1

        expected = row["expected_category"]
        predicted = result.category.value
        if predicted == expected:
            tp[expected] += 1
        else:
            fp[predicted] = fp.get(predicted, 0) + 1
            fn[expected] += 1

        snapshot = metrics.get_metrics_data()
        total_tokens += snapshot["total_tokens"]
        per_call_costs.append(snapshot["cost_usd"])

        # Exercise the guardrail path on the same ticket.
        drafts_checked += 1
        try:
            ai_service.suggest_response(row["subject"], row["description"], "")
        except ai_service.AIProviderError as exc:
            if "guardrail" in str(exc).lower():
                guardrail_blocks += 1
            else:
                provider_errors += 1

    n = len(data)
    f1_scores = []
    per_category = {}
    for category in categories:
        hit = tp[category]
        false_pos = fp.get(category, 0)
        false_neg = fn[category]
        precision = hit / (hit + false_pos) if hit + false_pos else 0.0
        recall = hit / (hit + false_neg) if hit + false_neg else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        f1_scores.append(f1)
        per_category[category] = {
            "precision": round(precision, 3),
            "recall": round(recall, 3),
            "f1": round(f1, 3),
            "support": hit + false_neg,
        }

    correct = sum(tp.values())
    priced = [c for c in per_call_costs if c]
    model_is_priced = pricing.is_priced(model)

    return {
        "provider": ai_service.settings.ai_provider,
        "model": model,
        "dataset_size": n,
        "classification": {
            "accuracy": round(correct / n, 4) if n else 0.0,
            "macro_f1": round(sum(f1_scores) / len(f1_scores), 4) if f1_scores else 0.0,
            "per_category": per_category,
            "provider_errors": provider_errors,
        },
        "schema_validity": {
            "valid": schema_ok,
            "total": n,
            "rate": round(schema_ok / n, 4) if n else 0.0,
        },
        "guardrails": {
            "blocked": guardrail_blocks,
            "checked": drafts_checked,
            "block_rate": round(guardrail_blocks / drafts_checked, 4) if drafts_checked else 0.0,
        },
        "cost": {
            "model_priced": model_is_priced,
            "usd_per_ticket": round(statistics.fmean(priced), 8) if priced else None,
            "usd_total": round(sum(per_call_costs), 6),
            "unpriced_calls": 0 if model_is_priced else n,
            "pricing_table_version": pricing.PRICING_TABLE_VERSION,
            "pricing_as_of": pricing.PRICING_AS_OF,
        },
        "latency_ms": {
            "p50": round(_percentile(latencies, 50), 2),
            "p95": round(_percentile(latencies, 95), 2),
            "max": round(max(latencies), 2) if latencies else 0.0,
        },
        "tokens": {
            "total": total_tokens,
            "per_ticket": round(total_tokens / n, 1) if n else 0.0,
        },
    }
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round((pct / 100) * len(ordered) + 0.5)) - 1)
    return ordered[max(0, index)]
def check_regression(current: dict, baseline: dict) -> list[str]:
    """Human-readable regression failures; empty list means no regression."""
    problems: list[str] = []

    acc = current["classification"]["accuracy"]
    acc_base = baseline["classification"]["accuracy"]
    drop_pp = (acc_base - acc) * 100
    if drop_pp > MAX_ACCURACY_DROP_PP:
        problems.append(
            f"accuracy dropped {drop_pp:.1f}pp "
            f"({acc_base:.3f} -> {acc:.3f}, limit {MAX_ACCURACY_DROP_PP}pp)"
        )

    base_rate = baseline["guardrails"]["block_rate"]
    rise = current["guardrails"]["block_rate"] - base_rate
    if rise > MAX_GUARDRAIL_RATE_RISE:
        problems.append(
            f"guardrail block rate rose {rise:.3f} "
            f"({base_rate:.3f} -> {current['guardrails']['block_rate']:.3f})"
        )

    base_p95 = baseline["latency_ms"]["p95"]
    if base_p95 > 0 and current["latency_ms"]["p95"] > base_p95 * MAX_P95_LATENCY_RATIO:
        problems.append(
            f"p95 latency {current['latency_ms']['p95']}ms exceeds "
            f"{MAX_P95_LATENCY_RATIO}x baseline {base_p95}ms"
        )

    return problems


def render(report: dict) -> str:
    cls = report["classification"]
    lines = [
        f"Dataset: {report['dataset_size']} labeled tickets | "
        f"provider: {report['provider']} | model: {report['model'] or 'n/a'}",
        "",
        f"{'Accuracy':<26}{cls['accuracy']:.1%}",
        f"{'Macro-F1':<26}{cls['macro_f1']:.2f}",
        f"{'Provider errors':<26}{cls['provider_errors']}",
        "",
        f"{'category':<16}{'precision':>10}{'recall':>10}{'f1':>8}{'support':>9}",
    ]
    for category, scores in cls["per_category"].items():
        lines.append(
            f"{category:<16}{scores['precision']:>10.2f}{scores['recall']:>10.2f}"
            f"{scores['f1']:>8.2f}{scores['support']:>9}"
        )

    schema = report["schema_validity"]
    guard = report["guardrails"]
    lines += [
        "",
        f"{'Schema validity':<26}{schema['valid']}/{schema['total']} ({schema['rate']:.1%})",
        f"{'Guardrail blocks':<26}{guard['blocked']}/{guard['checked']} ({guard['block_rate']:.1%})",
        "",
        f"{'Tokens / ticket':<26}{report['tokens']['per_ticket']}",
    ]
    cost = report["cost"]
    if cost["model_priced"]:
        lines.append(f"{'USD / ticket (triage)':<26}{cost['usd_per_ticket']}")
        lines.append(f"{'USD total':<26}{cost['usd_total']}")
    else:
        lines.append(
            f"{'USD / ticket':<26}not priced - model "
            f"'{report['model'] or 'unknown'}' absent from pricing table"
        )
    lat = report["latency_ms"]
    lines += [
        f"{'Latency p50 (ms)':<26}{lat['p50']}",
        f"{'Latency p95 (ms)':<26}{lat['p95']}",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate the SupportDesk AI layer.")
    parser.add_argument("--limit", type=int, help="only evaluate the first N tickets")
    parser.add_argument("--json", action="store_true", help="print raw JSON")
    parser.add_argument("--check", action="store_true", help="fail on regression vs baseline.json")
    parser.add_argument(
        "--write-baseline", action="store_true", help="save this run as the baseline"
    )
    args = parser.parse_args()

    report = run_suite(limit=args.limit)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render(report))

    if args.write_baseline:
        BASELINE.write_text(json.dumps(report, indent=2) + "\n")
        print(f"\nBaseline written to {BASELINE}")

    if args.check:
        if not BASELINE.exists():
            print(
                f"\nNo baseline at {BASELINE}. Run with --write-baseline to create one.",
                file=sys.stderr,
            )
            return 1
        baseline = json.loads(BASELINE.read_text())
        problems = check_regression(report, baseline)
        if problems:
            print("\nREGRESSION DETECTED:", file=sys.stderr)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
            return 1
        print("\nNo regression vs baseline.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())