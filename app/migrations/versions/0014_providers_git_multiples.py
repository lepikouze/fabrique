"""providers Git multiples : chaque repo est rattaché à un provider précis, plus seulement à un type

Un même GitLab peut être déclaré deux fois (même adresse, deux tokens : repos confidentiels d'un côté, accès complet de
l'autre), à côté d'un autre GitLab. Chaque provider reçoit un identifiant tiré de son nom ; chaque repo existant est
rattaché au provider actuel de son type (rien ne change pour lui). Un même chemin peut exister chez deux providers :
l'unicité porte sur (provider, chemin).

Revision ID: 0014
Revises: 0013
"""
import json, re, unicodedata
from alembic import op
import sqlalchemy as sa

revision, down_revision, branch_labels, depends_on = "0014", "0013", None, None


def _slug(name, taken):
    base = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    base = re.sub(r"[^a-z0-9]+", "-", base).strip("-")[:36] or "git"
    out, n = base, 2
    while out in taken:
        out, n = f"{base}-{n}", n + 1
    return out


def upgrade():
    # La contrainte d'unicité d'origine n'a pas de nom : la convention de nommage permet à Alembic de la retrouver (SQLite).
    with op.batch_alter_table("repos", naming_convention={"uq": "uq_%(table_name)s_%(column_0_name)s"}) as b:
        b.add_column(sa.Column("git_id", sa.String(60), nullable=False, server_default=""))
        b.drop_constraint("uq_repos_slug", type_="unique")
        b.create_unique_constraint("uq_repos_git_slug", ["git_id", "slug"])
    c = op.get_bind()
    row = c.execute(sa.text("SELECT value FROM settings WHERE key = 'git'")).first()
    git = row[0] if row else []
    if isinstance(git, str):
        git = json.loads(git or "[]")
    taken, first = set(), {}
    for g in git or []:
        if not g.get("id"):
            g["id"] = _slug(g.get("name") or g.get("type"), taken)
        taken.add(g["id"])
        first.setdefault(g.get("type"), g["id"])
    if row:
        c.execute(sa.text("UPDATE settings SET value = :v WHERE key = 'git'"), {"v": json.dumps(git, ensure_ascii=False)})
    for typ, gid in first.items():
        c.execute(sa.text("UPDATE repos SET git_id = :g WHERE provider = :t"), {"g": gid, "t": typ})


def downgrade():
    with op.batch_alter_table("repos") as b:
        b.drop_constraint("uq_repos_git_slug", type_="unique")
        b.create_unique_constraint("uq_repos_slug", ["slug"])
        b.drop_column("git_id")
