"""Add outbox_events, processed_events, and ticket SLA columns.

Revision ID: e4b82c5f1a30
Revises: e4f82a6d17c3 (re-chained to single head; was d3a71f4c9b20 before the
audit_events migration landed in parallel — both only add objects, so linear
order is safe)
Create Date: 2026-10-05
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e4b82c5f1a30'
down_revision: Union[str, Sequence[str], None] = 'e4f82a6d17c3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Ticket SLA tracking columns
    op.add_column('tickets', sa.Column('first_response_due_at', sa.DateTime(), nullable=True))
    op.add_column('tickets', sa.Column('resolution_due_at', sa.DateTime(), nullable=True))
    op.add_column('tickets', sa.Column('first_responded_at', sa.DateTime(), nullable=True))
    op.add_column('tickets', sa.Column('resolved_at', sa.DateTime(), nullable=True))
    op.add_column('tickets', sa.Column('sla_status', sa.String(length=20), nullable=False, server_default='WITHIN_SLA'))
    op.add_column('tickets', sa.Column('escalation_level', sa.Integer(), nullable=False, server_default='0'))
    op.create_index('ix_tickets_first_response_due_at', 'tickets', ['first_response_due_at'])
    op.create_index('ix_tickets_resolution_due_at', 'tickets', ['resolution_due_at'])
    op.create_index('ix_tickets_sla_status', 'tickets', ['sla_status'])

    # 2. Transactional Outbox table
    op.create_table(
        'outbox_events',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('event_id', sa.String(length=64), nullable=False),
        sa.Column('event_type', sa.String(length=100), nullable=False),
        sa.Column('aggregate_type', sa.String(length=60), nullable=False, server_default='ticket'),
        sa.Column('aggregate_id', sa.String(length=60), nullable=False),
        sa.Column('payload', sa.Text(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='PENDING'),
        sa.Column('retry_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('published_at', sa.DateTime(), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index('ix_outbox_events_event_id', 'outbox_events', ['event_id'], unique=True)
    op.create_index('ix_outbox_events_event_type', 'outbox_events', ['event_type'])
    op.create_index('ix_outbox_events_aggregate_type', 'outbox_events', ['aggregate_type'])
    op.create_index('ix_outbox_events_aggregate_id', 'outbox_events', ['aggregate_id'])
    op.create_index('ix_outbox_events_status', 'outbox_events', ['status'])
    op.create_index('ix_outbox_events_status_created', 'outbox_events', ['status', 'created_at'])

    # 3. Processed events table (Consumer idempotency)
    op.create_table(
        'processed_events',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('event_id', sa.String(length=64), nullable=False),
        sa.Column('consumer_group', sa.String(length=100), nullable=False),
        sa.Column('processed_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index('ix_processed_events_event_id', 'processed_events', ['event_id'])
    op.create_index('ix_processed_events_consumer_group', 'processed_events', ['consumer_group'])
    op.create_index('ix_processed_events_dedup', 'processed_events', ['event_id', 'consumer_group'], unique=True)


def downgrade() -> None:
    op.drop_table('processed_events')
    op.drop_table('outbox_events')
    op.drop_index('ix_tickets_sla_status', table_name='tickets')
    op.drop_index('ix_tickets_resolution_due_at', table_name='tickets')
    op.drop_index('ix_tickets_first_response_due_at', table_name='tickets')
    op.drop_column('tickets', 'escalation_level')
    op.drop_column('tickets', 'sla_status')
    op.drop_column('tickets', 'resolved_at')
    op.drop_column('tickets', 'first_responded_at')
    op.drop_column('tickets', 'resolution_due_at')
    op.drop_column('tickets', 'first_response_due_at')
