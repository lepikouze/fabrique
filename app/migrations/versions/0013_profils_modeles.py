"""profils de modèles : ce qu'un job a retenu de son profil (nom, effort, modèle et effort des relecteurs)

Le modèle principal du job reste dans jobs.model. jobs.models garde le reste, figé à la création du job : un profil
modifié pendant un job ne change pas le job en cours. Vide pour les jobs d'avant : un seul modèle, comme avant.

Revision ID: 0013
Revises: 0012
"""
from alembic import op
import sqlalchemy as sa

revision, down_revision, branch_labels, depends_on = "0013", "0012", None, None


def upgrade():
    with op.batch_alter_table("jobs") as b:
        b.add_column(sa.Column("models", sa.JSON(), nullable=True))


def downgrade():
    with op.batch_alter_table("jobs") as b:
        b.drop_column("models")
