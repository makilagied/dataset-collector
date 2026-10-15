"""hit words

Revision ID: 9b1e4c2d7a10
Revises: 2902267afa4b
Create Date: 2026-10-15 12:00:00.000000
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "9b1e4c2d7a10"
down_revision = "2902267afa4b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "comment_features",
        sa.Column(
            "hit_words",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
    )
    op.create_table(
        "hit_words",
        sa.Column("word", sa.Text(), nullable=False),
        sa.Column("root", sa.String(), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("word"),
    )


def downgrade() -> None:
    op.drop_table("hit_words")
    op.drop_column("comment_features", "hit_words")
