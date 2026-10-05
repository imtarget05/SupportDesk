"""Tests for Transactional Outbox, Event Consumer Idempotency, DLQ, and SLA Engine."""

from datetime import datetime, timedelta
import json
import pytest
from sqlalchemy.orm import Session

from app.enums import TicketPriority, TicketStatus, UserRole
from app.models.outbox import OutboxEvent, ProcessedEvent
from app.models.ticket import Ticket
from app.models.user import User
from app.security import hash_password
from app.services.kafka_producer import KafkaEventProducer
from app.services.outbox import get_pending_events, record_outbox_event
from app.services.sla_service import (
    calculate_sla_due_dates,
    check_and_update_ticket_sla,
    evaluate_all_active_slas,
    get_sla_metrics,
)
from app.services.ticket_service import (
    add_message,
    assign_ticket,
    create_ticket,
    update_ticket,
)
from app.workers.event_consumer import EventConsumer
from app.workers.outbox_publisher import publish_outbox_batch


@pytest.fixture
def agent_user(db_session: Session) -> User:
    agent = User(
        name="Support Agent",
        email="agent_sla@example.com",
        password_hash=hash_password("AgentPass123!"),
        role=UserRole.AGENT.value,
    )
    db_session.add(agent)
    db_session.commit()
    db_session.refresh(agent)
    return agent


@pytest.fixture
def customer_user(db_session: Session) -> User:
    customer = User(
        name="Customer User",
        email="customer_sla@example.com",
        password_hash=hash_password("CustomerPass123!"),
        role=UserRole.CUSTOMER.value,
    )
    db_session.add(customer)
    db_session.commit()
    db_session.refresh(customer)
    return customer


def test_transactional_outbox_on_ticket_create(db_session: Session, customer_user: User):
    """Creating a ticket atomically inserts a TicketCreated outbox event."""
    ticket = create_ticket(
        db_session,
        subject="VPN connection failing",
        description="Unable to connect to internal VPN since morning",
        customer=customer_user,
    )

    assert ticket.id is not None
    assert ticket.first_response_due_at is not None
    assert ticket.resolution_due_at is not None
    assert ticket.sla_status == "WITHIN_SLA"

    # Query outbox
    events = (
        db_session.query(OutboxEvent)
        .filter(OutboxEvent.aggregate_id == str(ticket.id))
        .all()
    )
    assert len(events) == 1
    assert events[0].event_type == "TicketCreated"
    assert events[0].status == "PENDING"
    payload = json.loads(events[0].payload)
    assert payload["ticket_id"] == ticket.id
    assert payload["subject"] == "VPN connection failing"


def test_transactional_outbox_on_ticket_assign(
    db_session: Session, customer_user: User, agent_user: User
):
    """Assigning a ticket emits a TicketAssigned event in the same transaction."""
    ticket = create_ticket(
        db_session,
        subject="Database lock contention",
        description="Query timeouts on reporting replica",
        customer=customer_user,
    )
    assign_ticket(db_session, ticket, assignee_id=agent_user.id)

    events = (
        db_session.query(OutboxEvent)
        .filter(
            OutboxEvent.aggregate_id == str(ticket.id),
            OutboxEvent.event_type == "TicketAssigned",
        )
        .all()
    )
    assert len(events) == 1
    payload = json.loads(events[0].payload)
    assert payload["assignee_id"] == agent_user.id
    assert payload["assignee_name"] == agent_user.name


def test_transactional_outbox_on_status_and_priority_update(
    db_session: Session, customer_user: User
):
    """Status and priority changes emit domain events and adjust SLA deadlines."""
    ticket = create_ticket(
        db_session,
        subject="Payment gateway webhook timeout",
        description="Stripe callbacks returning 504 gateway timeout",
        customer=customer_user,
    )
    original_res_due = ticket.resolution_due_at

    # Elevate to URGENT
    update_ticket(db_session, ticket, priority=TicketPriority.URGENT)
    assert ticket.priority == TicketPriority.URGENT.value
    # Urgent SLA (4h) is shorter than Normal SLA (24h)
    assert ticket.resolution_due_at < original_res_due

    # Resolve ticket
    update_ticket(db_session, ticket, status=TicketStatus.IN_PROGRESS)
    update_ticket(db_session, ticket, status=TicketStatus.RESOLVED)
    assert ticket.resolved_at is not None
    assert ticket.sla_status == "WITHIN_SLA"

    events = [
        e.event_type
        for e in db_session.query(OutboxEvent)
        .filter(OutboxEvent.aggregate_id == str(ticket.id))
        .all()
    ]
    assert "TicketCreated" in events
    assert "TicketPriorityChanged" in events
    assert "TicketStatusChanged" in events
    assert "TicketResolved" in events


def test_outbox_publisher_batch(db_session: Session, customer_user: User):
    """Outbox publisher marks events as PUBLISHED and records them in producer log."""
    producer = KafkaEventProducer()
    ticket = create_ticket(
        db_session,
        subject="SSO SAML error",
        description="Assertion expired signature invalid",
        customer=customer_user,
    )

    pending = get_pending_events(db_session)
    assert len(pending) >= 1

    dispatched = publish_outbox_batch(db_session, producer=producer, batch_size=10)
    assert dispatched >= 1

    published = (
        db_session.query(OutboxEvent)
        .filter(OutboxEvent.aggregate_id == str(ticket.id))
        .first()
    )
    assert published.status == "PUBLISHED"
    assert published.published_at is not None

    # Verify event reached producer
    dispatched_events = producer.get_published_events()
    assert any(e["event_type"] == "TicketCreated" for e in dispatched_events)


def test_event_consumer_idempotency(db_session: Session):
    """Consumer rejects duplicates of the same event_id to prevent double side-effects."""
    consumer = EventConsumer(consumer_group="notification-service")
    call_count = 0

    def mock_handler(event_data):
        nonlocal call_count
        call_count += 1

    consumer.register_handler("TicketCreated", mock_handler)

    test_event = {
        "event_id": "evt-idempotency-1001",
        "event_type": "TicketCreated",
        "aggregate_id": "42",
        "payload": {"ticket_id": 42},
    }

    # First delivery
    res1 = consumer.process_event(db_session, test_event)
    assert res1["status"] == "processed"
    assert call_count == 1

    # Duplicate delivery (network retry / consumer rebalance)
    res2 = consumer.process_event(db_session, test_event)
    assert res2["status"] == "skipped"
    assert res2["reason"] == "already_processed"
    assert call_count == 1  # Handler was NOT executed again


def test_event_consumer_retry_and_dlq(db_session: Session):
    """Failing event handlers route to retry topic and ultimately to DLQ on max attempts."""
    producer = KafkaEventProducer()
    consumer = EventConsumer(consumer_group="ai-service", producer=producer)

    def failing_handler(event_data):
        raise RuntimeError("Downstream vector DB down")

    consumer.register_handler("TicketCreated", failing_handler)

    event = {
        "event_id": "evt-dlq-2001",
        "event_type": "TicketCreated",
        "aggregate_id": "99",
        "payload": {"ticket_id": 99},
    }

    # Attempt 1 -> retry
    res1 = consumer.process_event(db_session, event, retry_count=0)
    assert res1["status"] == "retry"
    assert res1["attempt"] == 1

    # Attempt 2 -> retry
    res2 = consumer.process_event(db_session, event, retry_count=1)
    assert res2["status"] == "retry"
    assert res2["attempt"] == 2

    # Attempt 3 -> DLQ
    res3 = consumer.process_event(db_session, event, retry_count=2)
    assert res3["status"] == "dlq"

    # Verify DLQ topic received the event
    published = producer.get_published_events()
    dlq_records = [p for p in published if p["topic"] == producer.topic_dlq]
    assert len(dlq_records) >= 1
    assert dlq_records[-1]["payload"]["event_id"] == "evt-dlq-2001"
    assert "Downstream vector DB down" in dlq_records[-1]["payload"]["error_reason"]


def test_sla_priority_targets_and_warning_detection(db_session: Session, customer_user: User):
    """SLA engine detects 80% threshold warning and records audit/outbox event."""
    ticket = create_ticket(
        db_session,
        subject="Production Latency Spike",
        description="API 99th percentile exceeded 2000ms",
        customer=customer_user,
    )
    # Set ticket creation 20 hours ago for a 24h normal resolution target (elapsed ratio > 80%)
    ticket.created_at = datetime.utcnow() - timedelta(hours=20)
    ticket.resolution_due_at = ticket.created_at + timedelta(hours=24)
    db_session.commit()

    result = check_and_update_ticket_sla(db_session, ticket)
    assert result["warning"] is True
    assert ticket.sla_status == "WARNING"

    # Outbox event emitted
    warning_event = (
        db_session.query(OutboxEvent)
        .filter(
            OutboxEvent.aggregate_id == str(ticket.id),
            OutboxEvent.event_type == "SLAWarningTriggered",
        )
        .first()
    )
    assert warning_event is not None


def test_sla_breach_detection_and_escalation(db_session: Session, customer_user: User):
    """Breached SLA triggers SLABreached event, auto-escalation level, and audit trail."""
    ticket = create_ticket(
        db_session,
        subject="Cluster Node NotReady",
        description="Worker node failed disk pressure threshold",
        customer=customer_user,
    )
    # Ticket deadline was 2 hours ago
    ticket.created_at = datetime.utcnow() - timedelta(hours=26)
    ticket.resolution_due_at = datetime.utcnow() - timedelta(hours=2)
    ticket.sla_status = "WITHIN_SLA"
    ticket.escalation_level = 0
    db_session.commit()

    result = check_and_update_ticket_sla(db_session, ticket)
    assert result["breached"] is True
    assert result["escalated"] is True
    assert ticket.sla_status == "BREACHED"
    assert ticket.escalation_level == 1

    events = [
        e.event_type
        for e in db_session.query(OutboxEvent)
        .filter(OutboxEvent.aggregate_id == str(ticket.id))
        .all()
    ]
    assert "SLABreached" in events
    assert "TicketEscalated" in events


def test_sla_metrics_calculation(db_session: Session, customer_user: User):
    """SLA dashboard metrics compute exact compliance and response averages from DB."""
    # Create compliant ticket
    t1 = create_ticket(
        db_session,
        subject="Help with password",
        description="Cannot find password reset link",
        customer=customer_user,
    )
    t1.first_responded_at = t1.created_at + timedelta(minutes=15)
    t1.resolved_at = t1.created_at + timedelta(minutes=45)
    t1.status = TicketStatus.RESOLVED.value
    t1.sla_status = "WITHIN_SLA"

    # Create breached ticket
    t2 = create_ticket(
        db_session,
        subject="Billing discrepancy",
        description="Overcharge on monthly invoice",
        customer=customer_user,
    )
    t2.resolution_due_at = t2.created_at - timedelta(hours=1)
    t2.sla_status = "BREACHED"

    db_session.commit()

    metrics = get_sla_metrics(db_session)
    assert metrics["total_tickets"] >= 2
    assert metrics["resolved_tickets"] >= 1
    assert metrics["active_breaches"] >= 1
    assert metrics["compliance_rate_percent"] == 100.0  # 1 of 1 resolved tickets was compliant
    assert metrics["avg_first_response_time_minutes"] > 0
    assert "by_priority" in metrics


def test_sla_and_outbox_api_endpoints(client, db_session: Session, agent_user: User):
    """Test HTTP API access to SLA metrics, evaluation, and outbox operations."""
    from app.security import create_access_token

    token = create_access_token(agent_user)
    headers = {"Authorization": f"Bearer {token}"}

    # 1. Get SLA metrics
    res_metrics = client.get("/sla/metrics", headers=headers)
    assert res_metrics.status_code == 200
    data = res_metrics.json()
    assert "compliance_rate_percent" in data
    assert "total_tickets" in data

    # 2. Trigger SLA evaluation
    res_eval = client.post("/sla/evaluate", headers=headers)
    assert res_eval.status_code == 200
    assert res_eval.json()["message"] == "SLA evaluation completed"

    # 3. Check outbox stats
    res_stats = client.get("/sla/outbox/stats", headers=headers)
    assert res_stats.status_code == 200
    stats = res_stats.json()
    assert "pending" in stats
    assert "published" in stats

    # 4. Trigger outbox publish
    res_pub = client.post("/sla/outbox/publish-now", headers=headers)
    assert res_pub.status_code == 200
    assert "dispatched_count" in res_pub.json()

