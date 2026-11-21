"""diff du job : le runner envoie le diff du commit vérifié, lu dans le dashboard par le relecteur

Une ligne par job (le dernier diff remplace l'ancien), dans une table à part pour ne pas alourdir la lecture des jobs.

Revision ID: 0015
Revises: 0014
"""
from alembic import op
import sqlalchemy as sa

revision, down_revision, branch_labels, depends_on = "0015", "0014", None, None


def upgrade():
    op.create_table(
        "diffs",
        sa.Column("job_id", sa.Integer, sa.ForeignKey("jobs.id"), primary_key=True),
        sa.Column("sha", sa.String(40), nullable=False),
        sa.Column("text", sa.Text, nullable=False, server_default=""),
        sa.Column("truncated", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("files", sa.JSON, nullable=False, server_default="[]"),
        sa.Column("at", sa.Float, nullable=False),
    )


def downgrade():
    op.drop_table("diffs")
