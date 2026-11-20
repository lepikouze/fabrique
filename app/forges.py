"""Providers Git (Settings → Providers Git) : plusieurs par type, chacun désigné par son nom.

Un même GitLab peut être déclaré deux fois (même adresse, deux tokens différents : par exemple un compte robot limité aux
repos confidentiels et un autre en accès complet), à côté d'un second GitLab. Chaque provider a un identifiant interne
tiré de son nom (jamais montré), et chaque repo est rattaché à un provider précis (repos.git_id) : le runner reçoit
l'adresse et le token DE CE provider, jamais ceux d'un autre provider du même type.

Règles : le nom est unique ; l'adresse peut se répéter ; le token ne peut pas servir à deux providers."""
from . import db
from .profiles import slug as _slug

TYPES = ("forgejo", "gitea", "gitlab", "github")   # Gitea : même API que Forgejo (Forgejo en est issu)


class Invalid(ValueError):
    pass


def listing(c):
    """Providers Git déclarés. Un provider d'avant la 0.7.1 sans identifiant en reçoit un, tiré de son nom."""
    git = [dict(g) for g in (db.get_setting(c, "git")[0] or []) if isinstance(g, dict) and g.get("type") in TYPES]
    taken = {g["id"] for g in git if g.get("id")}
    for g in git:
        if not g.get("id"):
            g["id"] = _slug(g.get("name") or g["type"], taken)
            taken.add(g["id"])
    return git


def get(c, gid):
    return next((g for g in listing(c) if g["id"] == gid), None) if gid else None


def resolve(c, value):
    """Provider désigné par son identifiant ; un type seul (« gitlab », ancienne forme) désigne le premier de ce type."""
    return get(c, value) or (next((g for g in listing(c) if g["type"] == value), None) if value in TYPES else None)


def for_repo(c, repo):
    """Provider d'un repo : celui auquel il est rattaché ; à défaut (repo d'avant le rattachement), le premier provider
    de son type, comme avant la 0.7.1."""
    return get(c, repo.get("git_id")) or next((g for g in listing(c) if g["type"] == repo.get("provider")), None)


def label(c, repo):
    """Nom affiché du provider d'un repo (« GitLab interne »), ou son type si aucun provider n'est déclaré."""
    g = for_repo(c, repo)
    return g["name"] if g else {"forgejo": "Forgejo", "gitea": "Gitea", "gitlab": "GitLab", "github": "GitHub"}.get(repo.get("provider"), repo.get("provider") or "")


def save(c, by, gid, name, typ, base, token_plain, creation, clear_token=False):
    """Ajoute (gid vide) ou modifie un provider. Le token est chiffré ; vide, l'actuel est gardé (sauf si l'adresse
    change : un token enregistré n'est jamais envoyé à une autre adresse). Renvoie l'identifiant."""
    from .providers import seal, unseal, Invalid as PInvalid
    name, base = " ".join((name or "").split())[:80], (base or "").strip().rstrip("/")[:200]
    git = listing(c)
    old = next((g for g in git if g["id"] == gid), None) if gid else None
    if gid and not old:
        raise Invalid("Provider inconnu.")
    typ = old["type"] if old else typ
    if not name or typ not in TYPES or not base.startswith("https://"):
        raise Invalid("Provider invalide : nom, type et adresse https.")
    if any(g["name"].lower() == name.lower() and g["id"] != gid for g in git):
        raise Invalid(f"Un provider s'appelle déjà « {name} » : choisis un autre nom.")
    if old and old.get("token") and old.get("base_url") != base and not token_plain:
        raise Invalid("L'adresse change : saisis à nouveau le token (un token enregistré n'est jamais envoyé à une nouvelle adresse).")
    if token_plain:
        for g in git:
            if g["id"] != gid and g.get("token"):
                try:
                    same = unseal(g["token"]) == token_plain
                except PInvalid:
                    same = False
                if same:
                    raise Invalid(f"Ce token sert déjà au provider « {g['name']} » : un token ne peut servir qu'à un seul provider.")
    try:
        sealed = seal(token_plain[:500]) if token_plain else ("" if clear_token else (old or {}).get("token", ""))
    except PInvalid as e:
        raise Invalid(str(e))
    gid = gid or _slug(name, {g["id"] for g in git})
    entry = {"id": gid, "name": name, "type": typ, "base_url": base, "token": sealed, "creation": creation}
    git = [g for g in git if g["id"] != gid] + [entry]
    db.set_setting(c, "git", git, by)
    if not old:   # premier provider de ce type : les repos de ce type encore sans rattachement le rejoignent
        c.execute(db.repos.update().where(db.repos.c.provider == typ, db.repos.c.git_id == "").values(git_id=gid))
    db.log_audit(c, by, "Settings : provider Git", detail=f"{name} ({typ})")
    return gid


def remove(c, by, gid):
    """Retire un provider. Refusé tant que des repos y sont rattachés : ils perdraient leur accès."""
    g = get(c, gid)
    if not g:
        raise Invalid("Provider inconnu.")
    used = [r["slug"] for r in db.rows(c, db.repos.select().where(db.repos.c.git_id == gid))]
    if used:
        raise Invalid(f"« {g['name']} » sert encore à {len(used)} repo(s) ({', '.join(used[:5])}) : retire-les d'abord.")
    db.set_setting(c, "git", [x for x in listing(c) if x["id"] != gid], by)
    db.log_audit(c, by, "Settings : provider Git retiré", detail=f"{g['name']} ({g['type']})")
