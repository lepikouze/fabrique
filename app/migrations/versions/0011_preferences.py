"""préférences personnelles (affichage des jobs), gardées sur le compte

Revision ID: 0011
Revises: 0010
"""
from alembic import op
import sqlalchemy as sa

revision, down_revision, branch_labels, depends_on = "0011", "0010", None, None


def upgrade():
    with op.batch_alter_table("users") as b:
        b.add_column(sa.Column("prefs", sa.JSON(), nullable=True))


def downgrade():
    with op.batch_alter_table("users") as b:
        b.drop_column("prefs")
