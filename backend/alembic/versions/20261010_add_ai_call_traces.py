"""Add ai_call_traces for per-call token, cost and latency accounting.

Revision ID: f5b1c8e05d92
Revises: c2a9f6d41b7e
Create Date: 2026-10-10
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f5b1c8e05d92'
down_revision: Union[str, Sequence[str], None] = 'c2a9f6d41b7e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create schema."""
    op.create_table(
        'ai_call_traces',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('request_id', sa.String(length=64), nullable=False),
        sa.Column('ticket_id', sa.Integer(), nullable=True),
        sa.Column('operation', sa.String(length=40), nullable=False),
        sa.Column('provider', sa.String(length=40), nullable=False),
        sa.Column('model', sa.String(length=100), nullable=False),
        sa.Column('prompt_tokens', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('completion_tokens', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('cost_usd', sa.Float(), nullable=True),
        sa.Column('pricing_table_version', sa.String(length=20), nullable=True),
        sa.Column('usage_estimated', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('latency_ms', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('outcome', sa.String(length=20), nullable=False),
        sa.Column('error_kind', sa.String(length=60), nullable=True),
        sa.Column('confidence', sa.Float(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['ticket_id'], ['tickets.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_ai_call_traces_request_id', 'ai_call_traces', ['request_id'])
    op.create_index('ix_ai_call_traces_ticket_id', 'ai_call_traces', ['ticket_id'])
    op.create_index('ix_ai_call_traces_operation', 'ai_call_traces', ['operation'])
    op.create_index('ix_ai_call_traces_provider', 'ai_call_traces', ['provider'])
    op.create_index('ix_ai_call_traces_model', 'ai_call_traces', ['model'])
    op.create_index('ix_ai_call_traces_outcome', 'ai_call_traces', ['outcome'])


def downgrade() -> None:
    """Drop schema."""
    op.drop_index('ix_ai_call_traces_outcome', table_name='ai_call_traces')
    op.drop_index('ix_ai_call_traces_model', table_name='ai_call_traces')
    op.drop_index('ix_ai_call_traces_provider', table_name='ai_call_traces')
    op.drop_index('ix_ai_call_traces_operation', table_name='ai_call_traces')
    op.drop_index('ix_ai_call_traces_ticket_id', table_name='ai_call_traces')
    op.drop_index('ix_ai_call_traces_request_id', table_name='ai_call_traces')
    op.drop_table('ai_call_traces')
