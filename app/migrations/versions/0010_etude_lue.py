"""étude rendue : instant où son rapport a été marqué comme lu (sort de l'inbox)

Revision ID: 0010
Revises: 0009
"""
from alembic import op
import sqlalchemy as sa

revision, down_revision, branch_labels, depends_on = "0010", "0009", None, None


def upgrade():
    with op.batch_alter_table("jobs") as b:
        b.add_column(sa.Column("report_seen", sa.Float(), nullable=True))


def downgrade():
    with op.batch_alter_table("jobs") as b:
        b.drop_column("report_seen")
