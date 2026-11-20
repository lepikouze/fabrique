"""repos vierges : marqués comme tels, et débloqués si leur onboarding les avait fait repasser en « pas prêt »

Un repo initialisé par la Fabrique porte une recipe (vide) sur sa branche principale : il est utilisable. Avant la 0.5.0,
l'onboarding relancé après sa première merge le faisait repasser en « pas prêt », et un onboarding en échec le bloquait.
Cette migration marque les repos vierges (settings.vierge) et remet en état « vierge » ceux qui avaient été initialisés
puis bloqués. Elle ne touche pas aux repos prêts, ni à ceux qui n'ont jamais été initialisés.

Revision ID: 0012
Revises: 0011
"""
import json
from alembic import op
import sqlalchemy as sa

revision, down_revision, branch_labels, depends_on = "0012", "0011", None, None

BLOCKED = ("failed", "pending", "analyzing", "waiting")


def _settings(v):
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            v = {}
    return dict(v or {})


def upgrade():
    c = op.get_bind()
    repos = c.execute(sa.text("SELECT id, onboarding, settings FROM repos")).fetchall()
    for rid, onboarding, settings in repos:
        s = _settings(settings)
        if onboarding == "blank":
            blank = True
        elif onboarding in BLOCKED:
            # initialisé par la Fabrique : son job d'onboarding s'est terminé par « Repo initialisé »
            blank = c.execute(sa.text("SELECT 1 FROM jobs WHERE repo_id = :r AND source = 'onboarding' AND stage = 'merged' "
                                      "AND status_text = 'Repo initialisé' LIMIT 1"), {"r": rid}).first() is not None
        else:
            blank = False
        if not blank or (s.get("vierge") and onboarding == "blank"):
            continue
        s["vierge"] = True
        vals = {"s": json.dumps(s, ensure_ascii=False), "r": rid}
        if onboarding == "blank":
            c.execute(sa.text("UPDATE repos SET settings = :s WHERE id = :r"), vals)
        else:
            # la recipe en vigueur est celle de la branche principale : la recipe vide posée à l'initialisation
            c.execute(sa.text("UPDATE repos SET settings = :s, onboarding = 'blank', recipe = :p WHERE id = :r"),
                      dict(vals, p=json.dumps({"setup": [], "checks": {}, "env": []})))


def downgrade():
    pass    # le marquage est sans effet pour une version antérieure
