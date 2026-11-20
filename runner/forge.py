"""Fournisseurs Git : Forgejo, GitLab et GitHub, même interface. Le jeton ne sort jamais du runner :
il est passé à git par des variables GIT_CONFIG_* (pas dans l'URL, pas dans .git/config), et seulement quand l'agent
ne tourne pas.

Interface : clone_url, git_env, pr_url, open_pr, pr (-> {"head": {"sha"}, "merge_commit_sha"}), upsert_comment, merge.
Le jeton du runner peut pousser des branches fabrique/*, ouvrir des PR/MR et les fusionner ; la branche principale est
protégée contre la poussée directe (Forgejo : règle de branche ; GitLab : « push » Maintainers, « merge » Developers ;
GitHub : règle de branche ou ruleset, indisponibles sur un dépôt privé en offre gratuite — le runner ne pousse de toute
façon que fabrique/*)."""
import base64, time
from urllib.parse import quote
import httpx
from . import config


class Forgejo:
    def __init__(self, base=None, token=None, transport=None):
        self.base = (base or config.GIT_BASE_URL).rstrip("/")
        self.token = token or config.GIT_TOKEN
        self.api = httpx.Client(base_url=self.base + "/api/v1", timeout=30, transport=transport,
                                headers={"Authorization": f"token {self.token}", "Accept": "application/json"})

    def me(self):
        """Compte du runner (en minuscules), lu une fois : le runner ne met à jour que SES commentaires."""
        if not getattr(self, "_me", ""):
            self._me = (self.account() or "").lower()
        return self._me

    def clone_url(self, slug):
        return f"{self.base}/{slug}.git"

    def git_env(self):
        """Variables qui donnent l'en-tête d'authentification à git le temps d'une commande."""
        return {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": f"http.{self.base}/.extraheader",
                "GIT_CONFIG_VALUE_0": f"Authorization: token {self.token}"}

    def pr_url(self, slug, n):
        return f"{self.base}/{slug}/pulls/{n}"

    def account(self):
        """Nom du compte du runner chez le fournisseur (pour dire à qui donner l'accès)."""
        return self.api.get("/user").json().get("login") or ""

    def open_pr(self, slug, head, base, title, body):
        r = self.api.post(f"/repos/{slug}/pulls", json={"head": head, "base": base, "title": title, "body": body})
        if r.status_code == 409 or (r.status_code == 422 and "exist" in r.text):
            for p in self.api.get(f"/repos/{slug}/pulls", params={"state": "open", "limit": 50}).json():
                # seulement une PR de CE dépôt (pas celle d'un fork qui porterait le même nom de branche)
                if p["head"]["ref"] == head and ((p["head"].get("repo") or {}).get("full_name") or "").lower() == slug.lower():
                    self.api.patch(f"/repos/{slug}/pulls/{p['number']}", json={"title": title, "body": body})
                    return p["number"]
        r.raise_for_status()
        return r.json()["number"]

    def pr(self, slug, n):
        r = self.api.get(f"/repos/{slug}/pulls/{n}")
        r.raise_for_status()
        return r.json()

    def create_repo(self, namespace, name, description=""):
        """Dépôt privé et vide dans l'organisation dédiée ; « existe déjà » n'est pas une erreur s'il est vide."""
        r = self.api.post(f"/orgs/{namespace}/repos", json={"name": name, "description": description, "private": True,
                                                            "auto_init": False, "default_branch": "main"})
        if r.status_code == 409:
            return "existe"
        r.raise_for_status()
        return "créé"

    def open_prs(self, slug):
        r = self.api.get(f"/repos/{slug}/pulls", params={"state": "open", "limit": 20})
        r.raise_for_status()
        return [{"number": p["number"], "title": p.get("title") or "", "branch": (p.get("head") or {}).get("ref") or ""} for p in r.json() or []]

    MARK = "<!-- fabrique:decision -->"

    def upsert_comment(self, slug, n, body):
        """Un seul commentaire de décision par PR, mis à jour sur place."""
        body = self.MARK + "\n" + body
        r = self.api.get(f"/repos/{slug}/issues/{n}/comments")
        r.raise_for_status()
        me = self.me()
        for cm in r.json() or []:
            if self.MARK in (cm.get("body") or "") and ((cm.get("user") or {}).get("login") or "").lower() == me:
                self.api.patch(f"/repos/{slug}/issues/comments/{cm['id']}", json={"body": body}).raise_for_status()
                return
        self.api.post(f"/repos/{slug}/issues/{n}/comments", json={"body": body}).raise_for_status()

    def merge(self, slug, n, sha, title, message=""):
        """Fusion « squash » épinglée au sha vérifié : refusée par Forgejo si la tête de la PR a bougé. message : corps du
        commit de fusion (ligne « Fabrique-Job: … »)."""
        r = self.api.post(f"/repos/{slug}/pulls/{n}/merge", json={"Do": "squash", "head_commit_id": sha, "MergeTitleField": title,
                                                                  "delete_branch_after_merge": True,
                                                                  **({"MergeMessageField": message} if message else {})})
        if r.status_code not in (200, 204):
            raise RuntimeError(f"fusion refusée par Forgejo ({r.status_code}) : {r.text[:200]}")
        return self.pr(slug, n).get("merge_commit_sha") or ""


class GitLab:
    """GitLab (API v4). Le slug est le chemin complet du projet (groupe/sous-groupe/projet) ; le numéro est l'iid de la MR."""
    MARK = Forgejo.MARK

    def __init__(self, base=None, token=None, transport=None):
        self.base = (base or config.GITLAB_BASE_URL).rstrip("/")
        self.token = token or config.GITLAB_TOKEN
        self.api = httpx.Client(base_url=self.base + "/api/v4", timeout=30, transport=transport,
                                headers={"PRIVATE-TOKEN": self.token, "Accept": "application/json"})

    @staticmethod
    def _p(slug):
        return "/projects/" + quote(slug, safe="")

    def me(self):
        """Compte du runner (en minuscules), lu une fois : le runner ne met à jour que SES commentaires."""
        if not getattr(self, "_me", ""):
            self._me = (self.account() or "").lower()
        return self._me

    def clone_url(self, slug):
        return f"{self.base}/{slug}.git"

    def git_env(self):
        basic = base64.b64encode(f"oauth2:{self.token}".encode()).decode()
        return {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": f"http.{self.base}/.extraheader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}"}

    def pr_url(self, slug, n):
        return f"{self.base}/{slug}/-/merge_requests/{n}"

    def account(self):
        return self.api.get("/user").json().get("username") or ""

    def open_pr(self, slug, head, base, title, body):
        r = self.api.post(f"{self._p(slug)}/merge_requests", json={"source_branch": head, "target_branch": base, "title": title,
                                                                    "description": body, "remove_source_branch": True})
        if r.status_code == 409:   # une MR ouverte existe déjà pour cette branche : on la met à jour
            for m in self.api.get(f"{self._p(slug)}/merge_requests", params={"state": "opened", "source_branch": head}).json():
                if m.get("source_project_id") != m.get("target_project_id"):   # MR venue d'un fork : pas la nôtre
                    continue
                self.api.put(f"{self._p(slug)}/merge_requests/{m['iid']}", json={"title": title, "description": body}).raise_for_status()
                return m["iid"]
        r.raise_for_status()
        return r.json()["iid"]

    def pr(self, slug, n):
        r = self.api.get(f"{self._p(slug)}/merge_requests/{n}")
        r.raise_for_status()
        m = r.json()
        return {"head": {"sha": m.get("sha") or ""}, "state": m.get("state"),
                "merge_commit_sha": m.get("merge_commit_sha") or m.get("squash_commit_sha") or ""}

    def create_repo(self, namespace, name, description=""):
        g = self.api.get("/groups/" + quote(namespace, safe=""))
        g.raise_for_status()
        r = self.api.post("/projects", json={"name": name, "path": name, "namespace_id": g.json()["id"], "visibility": "private",
                                             "description": description, "initialize_with_readme": False, "default_branch": "main"})
        if r.status_code == 400 and "has already been taken" in r.text:
            return "existe"
        r.raise_for_status()
        return "créé"

    def open_prs(self, slug):
        r = self.api.get(f"{self._p(slug)}/merge_requests", params={"state": "opened", "per_page": 20})
        r.raise_for_status()
        return [{"number": m["iid"], "title": m.get("title") or "", "branch": m.get("source_branch") or ""} for m in r.json() or []]

    def upsert_comment(self, slug, n, body):
        body = self.MARK + "\n" + body
        r = self.api.get(f"{self._p(slug)}/merge_requests/{n}/notes", params={"per_page": 100, "sort": "asc"})
        r.raise_for_status()
        me = self.me()
        for nt in r.json() or []:
            if self.MARK in (nt.get("body") or "") and ((nt.get("author") or {}).get("username") or "").lower() == me:
                self.api.put(f"{self._p(slug)}/merge_requests/{n}/notes/{nt['id']}", json={"body": body}).raise_for_status()
                return
        self.api.post(f"{self._p(slug)}/merge_requests/{n}/notes", json={"body": body}).raise_for_status()

    def merge(self, slug, n, sha, title, wait=2.0, message=""):
        """Fusion « squash » épinglée au sha vérifié (paramètre sha : GitLab refuse si la tête a bougé)."""
        full = title + (f"\n\n{message}" if message else "")
        for attempt in range(4):
            r = self.api.put(f"{self._p(slug)}/merge_requests/{n}/merge",
                             json={"sha": sha, "squash": True, "squash_commit_message": full, "should_remove_source_branch": True})
            if r.status_code == 200:
                m = r.json()
                return m.get("merge_commit_sha") or m.get("squash_commit_sha") or ""
            if r.status_code in (405, 422) and attempt < 3:   # mergeabilité encore en calcul juste après une poussée
                time.sleep(wait)
                continue
            raise RuntimeError(f"fusion refusée par GitLab ({r.status_code}) : {r.text[:200]}")


class GitHub:
    """GitHub (API REST v3, ou GitHub Enterprise via GITHUB_API_URL). Le numéro est celui de la PR."""
    MARK = Forgejo.MARK

    def __init__(self, base=None, api=None, token=None, transport=None):
        self.base = (base or config.GITHUB_BASE_URL).rstrip("/")
        self.token = token or config.GITHUB_TOKEN
        self.api = httpx.Client(base_url=(api or config.GITHUB_API_URL).rstrip("/"), timeout=30, transport=transport,
                                headers={"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                                         "X-GitHub-Api-Version": "2022-11-28"})

    def me(self):
        """Compte du runner (en minuscules), lu une fois : le runner ne met à jour que SES commentaires."""
        if not getattr(self, "_me", ""):
            self._me = (self.account() or "").lower()
        return self._me

    def clone_url(self, slug):
        return f"{self.base}/{slug}.git"

    def git_env(self):
        basic = base64.b64encode(f"x-access-token:{self.token}".encode()).decode()
        return {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": f"http.{self.base}/.extraheader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}"}

    def pr_url(self, slug, n):
        return f"{self.base}/{slug}/pull/{n}"

    def account(self):
        return self.api.get("/user").json().get("login") or ""

    def open_pr(self, slug, head, base, title, body):
        r = self.api.post(f"/repos/{slug}/pulls", json={"head": head, "base": base, "title": title, "body": body})
        if r.status_code == 422 and "already exists" in r.text:   # une PR ouverte existe déjà pour cette branche
            owner = slug.split("/")[0]
            for p in self.api.get(f"/repos/{slug}/pulls", params={"state": "open", "head": f"{owner}:{head}"}).json():
                self.api.patch(f"/repos/{slug}/pulls/{p['number']}", json={"title": title, "body": body}).raise_for_status()
                return p["number"]
        r.raise_for_status()
        return r.json()["number"]

    def pr(self, slug, n):
        r = self.api.get(f"/repos/{slug}/pulls/{n}")
        r.raise_for_status()
        p = r.json()
        return {"head": {"sha": p["head"]["sha"], "ref": p["head"]["ref"]}, "state": p.get("state"),
                "merge_commit_sha": p.get("merge_commit_sha") if p.get("merged") else ""}

    def create_repo(self, namespace, name, description=""):
        me = self.api.get("/user").json().get("login", "")
        url = "/user/repos" if namespace == me else f"/orgs/{namespace}/repos"
        r = self.api.post(url, json={"name": name, "description": description, "private": True, "auto_init": False})
        if r.status_code == 422 and "already exists" in r.text:
            return "existe"
        r.raise_for_status()
        return "créé"

    def open_prs(self, slug):
        r = self.api.get(f"/repos/{slug}/pulls", params={"state": "open", "per_page": 20})
        r.raise_for_status()
        return [{"number": p["number"], "title": p.get("title") or "", "branch": (p.get("head") or {}).get("ref") or ""} for p in r.json() or []]

    def upsert_comment(self, slug, n, body):
        body = self.MARK + "\n" + body
        r = self.api.get(f"/repos/{slug}/issues/{n}/comments", params={"per_page": 100})
        r.raise_for_status()
        me = self.me()
        for cm in r.json() or []:
            if self.MARK in (cm.get("body") or "") and ((cm.get("user") or {}).get("login") or "").lower() == me:
                self.api.patch(f"/repos/{slug}/issues/comments/{cm['id']}", json={"body": body}).raise_for_status()
                return
        self.api.post(f"/repos/{slug}/issues/{n}/comments", json={"body": body}).raise_for_status()

    def merge(self, slug, n, sha, title, wait=2.0, message=""):
        """Fusion « squash » épinglée au sha vérifié (GitHub refuse avec 409 si la tête a bougé), puis branche supprimée."""
        for attempt in range(4):
            r = self.api.put(f"/repos/{slug}/pulls/{n}/merge", json={"sha": sha, "merge_method": "squash", "commit_title": title,
                                                                     **({"commit_message": message} if message else {})})
            if r.status_code == 200:
                merged = r.json().get("sha") or ""
                try:
                    ref = self.pr(slug, n)["head"]["ref"]
                    if ref.startswith("fabrique/"):
                        self.api.delete(f"/repos/{slug}/git/refs/heads/{ref}")
                except Exception:
                    pass
                return merged
            if r.status_code == 405 and attempt < 3 and "not mergeable" not in r.text.lower():   # mergeabilité en calcul
                time.sleep(wait)
                continue
            raise RuntimeError(f"fusion refusée par GitHub ({r.status_code}) : {r.text[:200]}")


def make(provider, base=None, token=None):
    """Client d'un fournisseur. Adresse et jeton : ceux du panneau s'ils sont donnés, sinon les variables du runner."""
    if provider == "gitlab":
        g = GitLab(base=base, token=token)
    elif provider == "github":
        api = None
        if base and base.rstrip("/") != "https://github.com":   # GitHub Enterprise : API sous /api/v3
            api = base.rstrip("/") + "/api/v3"
        g = GitHub(base=base, api=api, token=token)
    elif provider in ("forgejo", "gitea"):   # Gitea : même API que Forgejo
        g = Forgejo(base=base, token=token)
    else:
        raise RuntimeError(f"fournisseur Git non pris en charge par ce runner : {provider}")
    return g
