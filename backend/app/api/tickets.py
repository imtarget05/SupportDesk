from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app import rate_limit
from app.config import settings
from app.database import get_db
from app.deps import get_current_user, get_optional_user, require_agent
from app.enums import TicketCategory, TicketPriority, TicketStatus
from app.models import User
from app.schemas import (
    MessageCreate,
    MessageOut,
    TicketCreate,
    TicketDetailOut,
    TicketOut,
    TicketPage,
    TicketUpdate,
)
from app.services import ticket_service, email_service as email_svc
from app.services.state_machine import InvalidTransition

router = APIRouter(prefix="/api/tickets", tags=["tickets"])


@router.post("", response_model=TicketOut, status_code=status.HTTP_201_CREATED)
def create_ticket(
    body: TicketCreate,
    request: Request,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_optional_user),
) -> TicketOut:
    if user is not None:
        if user.role != "customer":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only customers can create tickets",
            )
        customer = user
    else:
        # Guest submission without an account is a deliberate product decision
        # (the public contact form must work before signup), so the only
        # identity available is the peer address, and that is the bucket key.
        # The limit bounds row creation; a body that fails schema validation is
        # rejected by FastAPI before this handler runs and creates nothing, so
        # it is deliberately not charged against the budget.
        peer = request.client.host if request.client else "unknown"
        try:
            rate_limit.check(
                f"ticket-create:{peer}",
                limit=settings.ticket_create_rate_limit,
                window_s=settings.ticket_create_rate_window_s,
            )
        except rate_limit.RateLimitExceeded as exc:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Too many guest tickets; retry in {exc.retry_after_s}s",
                headers={"Retry-After": str(exc.retry_after_s)},
            )
        if not body.customer_email:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="customer_email is required when submitting without an account",
            )
        customer = ticket_service.find_or_create_customer(
            db, body.customer_email, body.customer_name
        )

    ticket = ticket_service.create_ticket(
        db, subject=body.subject, description=body.description, customer=customer
    )
    return TicketOut.model_validate(ticket)


@router.get("", response_model=TicketPage)
def list_tickets(
    status_filter: TicketStatus | None = Query(default=None, alias="status"),
    priority: TicketPriority | None = None,
    category: TicketCategory | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> TicketPage:
    tickets, total = ticket_service.list_tickets(
        db,
        viewer=user,
        status=status_filter,
        priority=priority,
        category=category,
        page=page,
        page_size=page_size,
    )
    return TicketPage(
        items=[TicketOut.model_validate(t) for t in tickets],
        total=total,
        page=page,
        page_size=page_size,
    )


def _load_visible_ticket(ticket_id: int, db: Session, user: User):
    ticket = ticket_service.get_ticket_or_none(db, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")
    try:
        ticket_service.ensure_ticket_visible(ticket, user)
    except PermissionError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Not allowed to view this ticket"
        )
    return ticket


@router.get("/{ticket_id}", response_model=TicketDetailOut)
def get_ticket(
    ticket_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> TicketDetailOut:
    ticket = _load_visible_ticket(ticket_id, db, user)
    return TicketDetailOut.model_validate(ticket)


@router.patch("/{ticket_id}", response_model=TicketOut)
def update_ticket(
    ticket_id: int,
    body: TicketUpdate,
    db: Session = Depends(get_db),
    agent: User = Depends(require_agent),
) -> TicketOut:
    ticket = ticket_service.get_ticket_or_none(db, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")
    try:
        ticket = ticket_service.update_ticket(
            db, ticket, status=body.status, priority=body.priority
        )
    except InvalidTransition as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return TicketOut.model_validate(ticket)


@router.post(
    "/{ticket_id}/messages", response_model=MessageOut, status_code=status.HTTP_201_CREATED
)
def add_message(
    ticket_id: int,
    body: MessageCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> MessageOut:
    ticket = _load_visible_ticket(ticket_id, db, user)
    try:
        message = ticket_service.add_message(db, ticket, user, body.content)
    except ticket_service.TicketRuleViolation as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if user.role == "agent":
        result = email_svc.send_agent_reply(
            to_email=ticket.customer.email if ticket.customer else None,
            ticket_id=ticket.id,
            subject=ticket.subject,
            body=body.content,
        )
        message.email_status = result.status
        db.add(message)
        db.commit()
        db.refresh(message)
    return MessageOut.model_validate(message)
