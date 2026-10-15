"""hit roots

Revision ID: 4f7a2b9c1e35
Revises: 9b1e4c2d7a10
Create Date: 2026-10-15 23:00:00.000000
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "4f7a2b9c1e35"
down_revision = "9b1e4c2d7a10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "comment_features",
        sa.Column(
            "hit_roots",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
    )
    op.create_table(
        "hit_roots",
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
        sa.PrimaryKeyConstraint("root"),
    )


def downgrade() -> None:
    op.drop_table("hit_roots")
    op.drop_column("comment_features", "hit_roots")
