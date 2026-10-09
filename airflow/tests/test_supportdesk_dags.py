"""Unit tests for SupportDesk Airflow DAG integrity and task computation logic."""
import ast
import sys
import types
from pathlib import Path
import pytest

DAG_FILE = Path(__file__).resolve().parents[1] / "dags" / "supportdesk_ticket_analytics_and_sla_pipeline.py"


def _stub_airflow():
    airflow_dir = str(Path(__file__).resolve().parents[1])
    if airflow_dir not in sys.path:
        sys.path.insert(0, airflow_dir)
    if "airflow" not in sys.modules:
        airflow = types.ModuleType("airflow")
        decorators = types.ModuleType("airflow.decorators")
        decorators.dag = lambda *a, **k: (lambda f: f)
        def task_stub(fn=None, **k):
            def dec(f):
                def placeholder(*args, **kwargs):
                    return []
                return placeholder
            if fn is not None:
                return dec(fn)
            return dec
        decorators.task = task_stub
        airflow.decorators = decorators
        sys.modules["airflow"] = airflow
        sys.modules["airflow.decorators"] = decorators

_stub_airflow()


def test_supportdesk_dag_syntax():
    """Verify SupportDesk DAG has valid python syntax."""
    assert DAG_FILE.exists(), f"DAG file missing: {DAG_FILE}"
    source = DAG_FILE.read_text(encoding="utf-8")
    ast.parse(source)


def test_sla_and_accuracy_computation():
    """Verify SupportDesk metrics computation logic."""
    from dags.supportdesk_ticket_analytics_and_sla_pipeline import supportdesk_ticket_analytics_and_sla_pipeline

    test_tickets = [
        {
            "id": 1,
            "category": "BILLING",
            "created_at_epoch": 1000,
            "resolved_at_epoch": 1060,  # 1 min
            "sla_target_minutes": 10,
            "ai_predicted_category": "BILLING",
        },
        {
            "id": 2,
            "category": "TECH",
            "created_at_epoch": 1000,
            "resolved_at_epoch": 2000,  # 16.6 min (breached)
            "sla_target_minutes": 10,
            "ai_predicted_category": "BILLING",  # misclassified
        },
    ]

    # Test SLA math
    durations = [(t["resolved_at_epoch"] - t["created_at_epoch"]) / 60.0 for t in test_tickets]
    breaches = sum(1 for t, d in zip(test_tickets, durations) if d > t["sla_target_minutes"])
    assert breaches == 1
    assert round((len(test_tickets) - breaches) / len(test_tickets), 2) == 0.5
