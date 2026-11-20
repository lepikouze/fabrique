"""etiquettes des runners

Revision ID: 0005
Revises: 0004
"""
from alembic import op
import sqlalchemy as sa


revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('runners', schema=None) as batch_op:
        batch_op.add_column(sa.Column('tags', sa.JSON(), nullable=False, server_default='[]'))


def downgrade():
    with op.batch_alter_table('runners', schema=None) as batch_op:
        batch_op.drop_column('tags')
