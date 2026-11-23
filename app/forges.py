"""Git providers (Settings → Git providers) : multiple per type, each designated by its name.

The same GitLab can be declared twice (same address, two different tokens: for example one robot account limited to
confidential repos and another with full access), alongside a second GitLab. Each provider has an internal identifier
drawn from its name (never shown), and each repo is attached to a specific provider (repos.git_id): the runner receives
the address and the token OF THIS provider, never those of another provider of the same type.

Rules: the name is unique; the address can be repeated; the token cannot be used for two providers."""
from .i18n import M
from . import db
from .profiles import slug as _slug

TYPES = ("forgejo", "gitea", "gitlab", "github")   # Gitea: same API as Forgejo (Forgejo is derived from it)


class Invalid(ValueError):
    pass


def listing(c):
    """Declared Git providers. A provider from before 0.7.1 without an identifier receives one, drawn from its name."""
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
    """Provider designated by its identifier; a single type ("gitlab", old form) designates the first of this type."""
    return get(c, value) or (next((g for g in listing(c) if g["type"] == value), None) if value in TYPES else None)


def for_repo(c, repo):
    """Repo provider: the one it is attached to; failing that (repo from before attachment), the first provider
    of its type, as before 0.7.1."""
    return get(c, repo.get("git_id")) or next((g for g in listing(c) if g["type"] == repo.get("provider")), None)


def label(c, repo):
    """Display name of a repo provider ("Internal GitLab"), or its type if no provider is declared."""
    g = for_repo(c, repo)
    return g["name"] if g else {"forgejo": "Forgejo", "gitea": "Gitea", "gitlab": "GitLab", "github": "GitHub"}.get(repo.get("provider"), repo.get("provider") or "")


def save(c, by, gid, name, typ, base, token_plain, creation, clear_token=False):
    """Adds (empty gid) or modifies a provider. The token is encrypted; if empty, the current one is kept (unless the address
    changes: a stored token is never sent to another address). Returns the identifier."""
    from .providers import seal, unseal, Invalid as PInvalid
    name, base = " ".join((name or "").split())[:80], (base or "").strip().rstrip("/")[:200]
    git = listing(c)
    old = next((g for g in git if g["id"] == gid), None) if gid else None
    if gid and not old:
        raise Invalid(M("Unknown provider."))
    typ = old["type"] if old else typ
    if not name or typ not in TYPES or not base.startswith("https://"):
        raise Invalid(M("Invalid provider: name, type and https address."))
    if any(g["name"].lower() == name.lower() and g["id"] != gid for g in git):
        raise Invalid(M("A provider is already called “{name}”: choose another name.", name=name))
    if old and old.get("token") and old.get("base_url") != base and not token_plain:
        raise Invalid(M("The address changes: enter the token again (a stored token is never sent to a new address)."))
    if token_plain:
        for g in git:
            if g["id"] != gid and g.get("token"):
                try:
                    same = unseal(g["token"]) == token_plain
                except PInvalid:
                    same = False
                if same:
                    raise Invalid(M("This token is already used by provider “{name}”: a token can serve only one provider.", name=g['name']))
    try:
        sealed = seal(token_plain[:500]) if token_plain else ("" if clear_token else (old or {}).get("token", ""))
    except PInvalid as e:
        raise Invalid(str(e))
    gid = gid or _slug(name, {g["id"] for g in git})
    entry = {"id": gid, "name": name, "type": typ, "base_url": base, "token": sealed, "creation": creation}
    git = [g for g in git if g["id"] != gid] + [entry]
    db.set_setting(c, "git", git, by)
    if not old:   # first provider of this type: repos of this type without any connection yet will join it
        c.execute(db.repos.update().where(db.repos.c.provider == typ, db.repos.c.git_id == "").values(git_id=gid))
    db.log_audit(c, by, M("Settings: Git provider"), detail=f"{name} ({typ})")
    return gid


def remove(c, by, gid):
    """Removes a provider. Refused as long as repos are attached to it: they would lose their access."""
    g = get(c, gid)
    if not g:
        raise Invalid(M("Unknown provider."))
    used = [r["slug"] for r in db.rows(c, db.repos.select().where(db.repos.c.git_id == gid))]
    if used:
        raise Invalid(M("“{name}” is still used by {count} repo(s) ({join}): remove them first.", name=g['name'], count=len(used), join=', '.join(used[:5])))
    db.set_setting(c, "git", [x for x in listing(c) if x["id"] != gid], by)
    db.log_audit(c, by, M("Settings: Git provider removed"), detail=f"{g['name']} ({g['type']})")
