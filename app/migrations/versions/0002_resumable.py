"""0.8.10: a job stopped by a limit (duration, turns, a looping model) keeps its work and can be resumed

Revision ID: 0002
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa

revision, down_revision, branch_labels, depends_on = "0002", "0001", None, None


def upgrade():
    with op.batch_alter_table("jobs") as b:
        b.add_column(sa.Column("resumable", sa.Boolean(), server_default=sa.text("0"), nullable=False))


def downgrade():
    with op.batch_alter_table("jobs") as b:
        b.drop_column("resumable")
