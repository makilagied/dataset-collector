"""dedupe hits

Revision ID: c5d8e1f2a904
Revises: 4f7a2b9c1e35
Create Date: 2026-10-16 10:00:00.000000

Existing comments get hit_key NULL; `collector dedupe` fills it and merges duplicates.
"""

import sqlalchemy as sa
from alembic import op

revision = "c5d8e1f2a904"
down_revision = "4f7a2b9c1e35"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("comments", sa.Column("hit_key", sa.Text(), nullable=True))
    op.add_column(
        "comments",
        sa.Column("occurrences", sa.Integer(), server_default=sa.text("1"), nullable=False),
    )
    op.create_index(
        "ux_comments_hit_key",
        "comments",
        ["hit_key"],
        unique=True,
        postgresql_where=sa.text("hit_key <> ''"),
    )
    op.add_column(
        "comment_features",
        sa.Column("hit_weight", sa.Integer(), server_default=sa.text("1"), nullable=False),
    )
    op.create_table(
        "comment_duplicates",
        sa.Column("ig_comment_id", sa.String(), nullable=False),
        sa.Column("comment_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column(
            "seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["comment_id"], ["comments.id"]),
        sa.PrimaryKeyConstraint("ig_comment_id"),
    )
    op.create_index(
        "ix_comment_duplicates_comment_id", "comment_duplicates", ["comment_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_comment_duplicates_comment_id", table_name="comment_duplicates")
    op.drop_table("comment_duplicates")
    op.drop_column("comment_features", "hit_weight")
    op.drop_index("ux_comments_hit_key", table_name="comments")
    op.drop_column("comments", "occurrences")
    op.drop_column("comments", "hit_key")
