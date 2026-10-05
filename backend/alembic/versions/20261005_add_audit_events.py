"""Add audit_events and tickets.assignee_id.

Revision ID: e4f82a6d17c3
Revises: d3a71f4c9b20
Create Date: 2026-10-05
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e4f82a6d17c3'
down_revision: Union[str, Sequence[str], None] = 'd3a71f4c9b20'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'audit_events',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('ticket_id', sa.Integer(), nullable=False),
        sa.Column('action', sa.String(length=60), nullable=False),
        sa.Column('workflow_id', sa.String(length=120), nullable=False, server_default=''),
        sa.Column('stage', sa.String(length=60), nullable=False, server_default=''),
        sa.Column('actor', sa.String(length=120), nullable=False, server_default='system'),
        sa.Column('details', sa.Text(), nullable=False, server_default='{}'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.ForeignKeyConstraint(['ticket_id'], ['tickets.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_audit_events_ticket_id', 'audit_events', ['ticket_id'])
    op.create_index('ix_audit_events_action', 'audit_events', ['action'])
    op.create_index(
        'ix_audit_events_ticket_created', 'audit_events', ['ticket_id', 'created_at']
    )


def downgrade() -> None:
    op.drop_index('ix_audit_events_ticket_created', table_name='audit_events')
    op.drop_index('ix_audit_events_action', table_name='audit_events')
    op.drop_index('ix_audit_events_ticket_id', table_name='audit_events')
    op.drop_table('audit_events')
