"""mode étude : rapport rendu par l'agent (job sans branche ni PR)

Revision ID: 0008
Revises: 0007
"""
from alembic import op
import sqlalchemy as sa

revision, down_revision, branch_labels, depends_on = "0008", "0007", None, None


def upgrade():
    with op.batch_alter_table("jobs") as b:
        b.add_column(sa.Column("report", sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table("jobs") as b:
        b.drop_column("report")
