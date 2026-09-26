"""Ticket domain logic. Routers stay thin; rules live here."""

from sqlalchemy import func, update
from sqlalchemy.orm import Session

from app.enums import (
    TicketCategory,
    TicketPriority,
    TicketStatus,
    UserRole,
)
from app.models import Message, Ticket, User
from app.security import hash_password
from app.services.state_machine import InvalidTransition, validate_transition


class TicketRuleViolation(Exception):
    """Domain rule that maps to HTTP 409."""


def find_or_create_customer(db: Session, email: str, name: str | None) -> User:
    email = email.strip().lower()
    user = db.query(User).filter(User.email == email).first()
    if user is not None:
        return user
    user = User(
        name=(name.strip() if name else email.split("@")[0]),
        email=email,
        password_hash=None,  # anonymous submitter has no usable password yet
        role=UserRole.CUSTOMER.value,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def create_ticket(db: Session, *, subject: str, description: str, customer: User) -> Ticket:
    ticket = Ticket(
        customer_id=customer.id,
        subject=subject.strip(),
        description=description.strip(),
        status=TicketStatus.OPEN.value,
        category=TicketCategory.UNKNOWN.value,
        priority=TicketPriority.NORMAL.value,
    )
    db.add(ticket)
    db.commit()
    db.refresh(ticket)
    return ticket


def list_tickets(
    db: Session,
    *,
    viewer: User,
    status: TicketStatus | None = None,
    priority: TicketPriority | None = None,
    category: TicketCategory | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[Ticket], int]:
    query = db.query(Ticket)
    if viewer.role != UserRole.AGENT.value:
        query = query.filter(Ticket.customer_id == viewer.id)
    if status is not None:
        query = query.filter(Ticket.status == status.value)
    if priority is not None:
        query = query.filter(Ticket.priority == priority.value)
    if category is not None:
        query = query.filter(Ticket.category == category.value)

    total = query.count()
    page = max(page, 1)
    page_size = min(max(page_size, 1), 100)
    tickets = (
        query.order_by(Ticket.created_at.desc(), Ticket.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return tickets, total


def get_ticket_or_none(db: Session, ticket_id: int) -> Ticket | None:
    return db.get(Ticket, ticket_id)


def ensure_ticket_visible(ticket: Ticket, viewer: User) -> None:
    if viewer.role != UserRole.AGENT.value and ticket.customer_id != viewer.id:
        raise PermissionError("Not allowed to view this ticket")


def update_ticket(
    db: Session,
    ticket: Ticket,
    *,
    status: TicketStatus | None = None,
    priority: TicketPriority | None = None,
) -> Ticket:
    """Agent-only lifecycle control. Priority is agent-adjustable anytime.

    A status change is written with a conditional UPDATE guarded on the status
    that was validated against, so two agents patching the same ticket cannot
    silently overwrite each other. Under PostgreSQL's READ COMMITTED isolation
    a plain read-then-write is last-write-win; here the guard turns a losing
    race into 0 rows affected, which is reported as a conflict instead.
    """
    if status is not None:
        current = TicketStatus(ticket.status)
        if status != current:
            validate_transition(current, status)  # raises InvalidTransition
            rows = db.execute(
                update(Ticket)
                .where(Ticket.id == ticket.id, Ticket.status == current.value)
                .values(status=status.value)
                .execution_options(synchronize_session=False)
            ).rowcount
            if rows == 0:
                # Someone else moved the ticket between our read and our write.
                db.rollback()
                latest = db.get(Ticket, ticket.id)
                actual = TicketStatus(latest.status) if latest is not None else current
                db.expire(ticket)
                raise InvalidTransition(actual, status)
            # Drop the stale in-memory value so the flush below cannot re-issue
            # an unguarded UPDATE; the real value is re-read by the refresh().
            db.expire(ticket)
    if priority is not None:
        ticket.priority = priority.value
    db.commit()
    db.refresh(ticket)
    return ticket


def add_message(db: Session, ticket: Ticket, sender: User, content: str) -> Message:
    if ticket.status == TicketStatus.CLOSED.value:
        raise TicketRuleViolation("Cannot add messages to a closed ticket")

    message = Message(ticket_id=ticket.id, sender_id=sender.id, content=content.strip())
    db.add(message)

    # System rule (not AI): a customer reply reopens a WAITING ticket.
    if sender.role == UserRole.CUSTOMER.value and ticket.status == TicketStatus.WAITING.value:
        ticket.status = TicketStatus.IN_PROGRESS.value

    db.commit()
    db.refresh(message)
    return message


def get_stats(db: Session) -> dict:
    by_status = {s.value: 0 for s in TicketStatus}
    by_priority = {p.value: 0 for p in TicketPriority}
    by_category = {c.value: 0 for c in TicketCategory}

    for value, count in db.query(Ticket.status, func.count()).group_by(Ticket.status).all():
        by_status[value] = count
    for value, count in db.query(Ticket.priority, func.count()).group_by(Ticket.priority).all():
        by_priority[value] = count
    for value, count in db.query(Ticket.category, func.count()).group_by(Ticket.category).all():
        by_category[value] = count

    unresolved = (TicketStatus.OPEN.value, TicketStatus.IN_PROGRESS.value, TicketStatus.WAITING.value)
    high_open = (
        db.query(func.count())
        .filter(
            Ticket.priority.in_([TicketPriority.HIGH.value, TicketPriority.URGENT.value]),
            Ticket.status.in_(unresolved),
        )
        .scalar()
    )

    return {
        "total": sum(by_status.values()),
        "by_status": by_status,
        "by_priority": by_priority,
        "by_category": by_category,
        "high_priority_open": high_open,
    }
