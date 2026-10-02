"""Add embedder id and width to ticket_embeddings.

Revision ID: c2a9f6d41b7e
Revises: b7c4e2a91f03
Create Date: 2026-10-10
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c2a9f6d41b7e'
down_revision: Union[str, Sequence[str], None] = 'b7c4e2a91f03'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Record which embedder produced each stored vector.

    Existing rows are backfilled with the bag-of-words defaults, which is what
    they actually are: they were written by `embed()`, never by the HF path.
    """
    op.add_column(
        'ticket_embeddings',
        sa.Column('model', sa.String(length=100), nullable=True),
    )
    op.add_column(
        'ticket_embeddings',
        sa.Column('dim', sa.Integer(), nullable=True),
    )
    op.execute("UPDATE ticket_embeddings SET model = 'bow-sha256', dim = 128")
    # SQLite cannot add a NOT NULL column without a default; use batch_alter
    # to rebuild the table with the constraint the ORM model declares.
    with op.batch_alter_table('ticket_embeddings') as batch:
        batch.alter_column('model', existing_type=sa.String(length=100), nullable=False)
        batch.alter_column('dim', existing_type=sa.Integer(), nullable=False)


def downgrade() -> None:
    """Drop schema."""
    with op.batch_alter_table('ticket_embeddings') as batch:
        batch.drop_column('dim')
        batch.drop_column('model')
