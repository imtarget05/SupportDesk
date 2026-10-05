"""Add nullable assignee to tickets (agent assignment is persisted).

Revision ID: d3a71f4c9b20
Revises: f5b1c8e05d92
Create Date: 2026-10-05
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd3a71f4c9b20'
down_revision: Union[str, Sequence[str], None] = 'f5b1c8e05d92'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('tickets', sa.Column('assignee_id', sa.Integer(), nullable=True))
    # SQLite cannot ALTER constraints; the FK is enforced by the ORM and by
    # PostgreSQL in production. Only create the constraint off-SQLite.
    bind = op.get_bind()
    if bind is None or bind.dialect.name != 'sqlite':
        op.create_foreign_key(
            'fk_tickets_assignee_id', 'tickets', 'users',
            ['assignee_id'], ['id'],
        )
    op.create_index('ix_tickets_assignee_id', 'tickets', ['assignee_id'])


def downgrade() -> None:
    op.drop_index('ix_tickets_assignee_id', table_name='tickets')
    bind = op.get_bind()
    if bind is None or bind.dialect.name != 'sqlite':
        op.drop_constraint('fk_tickets_assignee_id', 'tickets', type_='foreignkey')
    op.drop_column('tickets', 'assignee_id')
