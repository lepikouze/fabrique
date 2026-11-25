"""Git providers: Forgejo, GitLab and GitHub, same interface. The token never leaves the runner: it is passed to git
through GIT_CONFIG_* variables (not in the URL, not in .git/config), and only while the agent is not running.

Interface: clone_url, git_env, pr_url, open_pr, pr (-> {"head": {"sha"}, "merge_commit_sha"}), upsert_comment, merge, close_pr.
The runner's token can push fabrique/* branches, open PRs/MRs and merge them; the main branch is protected against
direct pushes (Forgejo: branch rule; GitLab: "push" Maintainers, "merge" Developers; GitHub: branch rule or ruleset,
unavailable on a private repo on the free plan — the runner only ever pushes fabrique/* anyway)."""
try:
    from .texts import M
except ImportError:
    try:
        from texts import M
    except ImportError:
        def M(t, **p):
            return t.format(**p) if p else t
import base64, time
from urllib.parse import quote
import httpx
from . import config


class CreateRefused(RuntimeError):
    """A repo creation refused by the Git host, with the step, the HTTP status and the host's own reason."""


def host_reason(r):
    """The Git host's own words in an error response (JSON "message"/"error", or the start of the text)."""
    try:
        d = r.json()
        m = d.get("message") or d.get("error") or d.get("errors") if isinstance(d, dict) else d
        if isinstance(m, (dict, list)):
            m = str(m)
        return " ".join(str(m or "").split())[:200]
    except ValueError:
        return " ".join((r.text or "").split())[:200]


class Forgejo:
    def __init__(self, base=None, token=None, transport=None):
        self.base = (base or config.GIT_BASE_URL).rstrip("/")
        self.token = token or config.GIT_TOKEN
        self.api = httpx.Client(base_url=self.base + "/api/v1", timeout=30, transport=transport,
                                headers={"Authorization": f"token {self.token}", "Accept": "application/json"})

    def me(self):
        """Runner account (in lowercase), read once: the runner only updates ITS comments."""
        if not getattr(self, "_me", ""):
            self._me = (self.account() or "").lower()
        return self._me

    def clone_url(self, slug):
        return f"{self.base}/{slug}.git"

    def git_env(self):
        """Variables that provide the authentication header to git during a command."""
        return {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": f"http.{self.base}/.extraheader",
                "GIT_CONFIG_VALUE_0": f"Authorization: token {self.token}"}

    def pr_url(self, slug, n):
        return f"{self.base}/{slug}/pulls/{n}"

    def account(self):
        """Account name of the runner at the Git provider (to specify to whom access should be granted)."""
        return self.api.get("/user").json().get("login") or ""

    def open_pr(self, slug, head, base, title, body):
        r = self.api.post(f"/repos/{slug}/pulls", json={"head": head, "base": base, "title": title, "body": body})
        if r.status_code == 409 or (r.status_code == 422 and "exist" in r.text):
            for p in self.api.get(f"/repos/{slug}/pulls", params={"state": "open", "limit": 50}).json():
                # only a PR from THIS repo (not one from a fork with the same branch name)
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
        """Private and empty repo in the dedicated organization; "already exists" is not an error if it is empty."""
        r = self.api.post(f"/orgs/{namespace}/repos", json={"name": name, "description": description, "private": True,
                                                            "auto_init": False, "default_branch": "main"})
        if r.status_code == 409:
            return "exists"
        if r.status_code == 404:
            raise CreateRefused(M("the organization “{ns}” cannot be found, or the bot account cannot see it ({status}): {why}",
                                  ns=namespace, status=r.status_code, why=host_reason(r)))
        if r.status_code in (401, 403):
            raise CreateRefused(M("the Git host refuses ({status}): the bot account may not create repos in “{ns}” (it needs the right to "
                                  "create repositories in this organization, and a token with write:organization): {why}",
                                  status=r.status_code, ns=namespace, why=host_reason(r)))
        if r.status_code >= 400:
            raise CreateRefused(M("the Git host refused the creation ({status}): {why}", status=r.status_code, why=host_reason(r)))
        return M("created")

    def open_prs(self, slug):
        r = self.api.get(f"/repos/{slug}/pulls", params={"state": "open", "limit": 20})
        r.raise_for_status()
        return [{"number": p["number"], "title": p.get("title") or "", "branch": (p.get("head") or {}).get("ref") or ""} for p in r.json() or []]

    MARK = "<!-- fabrique:decision -->"

    def upsert_comment(self, slug, n, body):
        """Only one decision comment per PR, updated in place."""
        body = self.MARK + "\n" + body
        r = self.api.get(f"/repos/{slug}/issues/{n}/comments")
        r.raise_for_status()
        me = self.me()
        for cm in r.json() or []:
            if self.MARK in (cm.get("body") or "") and ((cm.get("user") or {}).get("login") or "").lower() == me:
                self.api.patch(f"/repos/{slug}/issues/comments/{cm['id']}", json={"body": body}).raise_for_status()
                return
        self.api.post(f"/repos/{slug}/issues/{n}/comments", json={"body": body}).raise_for_status()

    def close_pr(self, slug, n, comment=""):
        """Closes a PR without merging it (merge refused in the dashboard), with a comment saying why."""
        if comment:
            self.api.post(f"/repos/{slug}/issues/{n}/comments", json={"body": comment}).raise_for_status()
        self.api.patch(f"/repos/{slug}/pulls/{n}", json={"state": "closed"}).raise_for_status()

    def merge(self, slug, n, sha, title, message=""):
        """Squash merge pinned to the verified sha: refused by Forgejo if the PR head has moved. message: merge
        commit body (line "Fabrique-Job: ...")."""
        r = self.api.post(f"/repos/{slug}/pulls/{n}/merge", json={"Do": "squash", "head_commit_id": sha, "MergeTitleField": title,
                                                                  "delete_branch_after_merge": True,
                                                                  **({"MergeMessageField": message} if message else {})})
        if r.status_code not in (200, 204):
            raise RuntimeError(M("merge refused by Forgejo ({status_code}): {p}", status_code=r.status_code, p=r.text[:200]))
        return self.pr(slug, n).get("merge_commit_sha") or ""


class GitLab:
    """GitLab (API v4). The slug is the full project path (group/sub-group/project); the number is the MR iid."""
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
        """Runner account (in lowercase), read once: the runner only updates ITS comments."""
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
        if r.status_code == 409:   # an open MR already exists for this branch: we update it
            for m in self.api.get(f"{self._p(slug)}/merge_requests", params={"state": "opened", "source_branch": head}).json():
                if m.get("source_project_id") != m.get("target_project_id"):   # MR coming from a fork: not ours
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
        # A group (or sub-group) or a personal namespace: /namespaces knows both.
        g = self.api.get("/namespaces/" + quote(namespace, safe=""))
        if g.status_code != 200:
            raise CreateRefused(M("GitLab cannot find the group “{ns}”, or the bot account is not a member of it ({status}): {why}",
                                  ns=namespace, status=g.status_code, why=host_reason(g)))
        r = self.api.post("/projects", json={"name": name, "path": name, "namespace_id": g.json()["id"], "visibility": "private",
                                             "description": description, "initialize_with_readme": False, "default_branch": "main"})
        if r.status_code == 400 and "has already been taken" in r.text:
            return "exists"
        if r.status_code in (401, 403):
            raise CreateRefused(M("GitLab refuses ({status}): the bot account may not create projects in “{ns}”. Give it the Maintainer role "
                                  "in this group, or allow Developers to create projects (group Settings → General → Permissions → "
                                  "Roles allowed to create projects): {why}", status=r.status_code, ns=namespace, why=host_reason(r)))
        if r.status_code >= 400:
            raise CreateRefused(M("GitLab refused the creation ({status}): {why}", status=r.status_code, why=host_reason(r)))
        return M("created")

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

    def close_pr(self, slug, n, comment=""):
        """Closes an MR without merging it (merge refused in the dashboard), with a note saying why."""
        if comment:
            self.api.post(f"{self._p(slug)}/merge_requests/{n}/notes", json={"body": comment}).raise_for_status()
        self.api.put(f"{self._p(slug)}/merge_requests/{n}", json={"state_event": "close"}).raise_for_status()

    def merge(self, slug, n, sha, title, wait=2.0, message=""):
        """"Squash" merge pinned to the verified sha (sha parameter: GitLab refuses if the head has moved)."""
        full = title + (f"\n\n{message}" if message else "")
        for attempt in range(4):
            r = self.api.put(f"{self._p(slug)}/merge_requests/{n}/merge",
                             json={"sha": sha, "squash": True, "squash_commit_message": full, "should_remove_source_branch": True})
            if r.status_code == 200:
                m = r.json()
                return m.get("merge_commit_sha") or m.get("squash_commit_sha") or ""
            if r.status_code in (405, 422) and attempt < 3:   # mergeability still being calculated right after a push
                time.sleep(wait)
                continue
            raise RuntimeError(M("merge refused by GitLab ({status_code}): {p}", status_code=r.status_code, p=r.text[:200]))


class GitHub:
    """GitHub (REST API v3, or GitHub Enterprise via GITHUB_API_URL). The number is that of the PR."""
    MARK = Forgejo.MARK

    def __init__(self, base=None, api=None, token=None, transport=None):
        self.base = (base or config.GITHUB_BASE_URL).rstrip("/")
        self.token = token or config.GITHUB_TOKEN
        self.api = httpx.Client(base_url=(api or config.GITHUB_API_URL).rstrip("/"), timeout=30, transport=transport,
                                headers={"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                                         "X-GitHub-Api-Version": "2022-11-28"})

    def me(self):
        """Runner account (in lowercase), read once: the runner only updates ITS comments."""
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
        if r.status_code == 422 and "already exists" in r.text:   # an open PR already exists for this branch
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
            return "exists"
        if r.status_code in (401, 403, 404):
            raise CreateRefused(M("GitHub refuses ({status}): the token may not create repos in “{ns}” (fine-grained token: Administration "
                                  "write on the account or the organization): {why}", status=r.status_code, ns=namespace, why=host_reason(r)))
        r.raise_for_status()
        return M("created")

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

    def close_pr(self, slug, n, comment=""):
        """Closes a PR without merging it (merge refused in the dashboard), with a comment saying why."""
        if comment:
            self.api.post(f"/repos/{slug}/issues/{n}/comments", json={"body": comment}).raise_for_status()
        self.api.patch(f"/repos/{slug}/pulls/{n}", json={"state": "closed"}).raise_for_status()

    def merge(self, slug, n, sha, title, wait=2.0, message=""):
        """Squash merge pinned to the verified sha (GitHub refuses with 409 if the head has moved), then branch deleted."""
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
            if r.status_code == 405 and attempt < 3 and "not mergeable" not in r.text.lower():   # mergeability being calculated
                time.sleep(wait)
                continue
            raise RuntimeError(M("merge refused by GitHub ({status_code}): {p}", status_code=r.status_code, p=r.text[:200]))


def make(provider, base=None, token=None):
    """Client of a Git provider. Address and token: those from the dashboard if provided, otherwise the runner's variables."""
    if provider == "gitlab":
        g = GitLab(base=base, token=token)
    elif provider == "github":
        api = None
        if base and base.rstrip("/") != "https://github.com":   # GitHub Enterprise: API under /api/v3
            api = base.rstrip("/") + "/api/v3"
        g = GitHub(base=base, api=api, token=token)
    elif provider in ("forgejo", "gitea"):   # Gitea: same API as Forgejo
        g = Forgejo(base=base, token=token)
    else:
        raise RuntimeError(M("Git provider not supported by this runner: {provider}", provider=provider))
    return g
