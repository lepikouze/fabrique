"""Client GitLab du runner : mêmes contrats que Forgejo (MR, commentaire de décision unique, merge épinglé au sha)."""
import base64, json
import httpx
import pytest
from runner import forge


def gitlab(handler):
    return forge.GitLab(base="https://gl.example", token="tok", transport=httpx.MockTransport(handler))


def test_git_env_uses_basic_auth_header_not_url():
    g = gitlab(lambda r: httpx.Response(500))
    env = g.git_env()
    assert g.clone_url("grp/sous/proj") == "https://gl.example/grp/sous/proj.git"      # pas de jeton dans l'URL
    assert env["GIT_CONFIG_KEY_0"] == "http.https://gl.example/.extraheader"
    assert base64.b64decode(env["GIT_CONFIG_VALUE_0"].split()[-1]).decode() == "oauth2:tok"
    assert g.pr_url("grp/proj", 7) == "https://gl.example/grp/proj/-/merge_requests/7"


def test_open_mr_new_then_existing():
    calls = []

    def handler(req):
        calls.append((req.method, req.url.raw_path.decode()))
        assert req.headers["PRIVATE-TOKEN"] == "tok"
        if req.method == "POST" and len([c for c in calls if c[0] == "POST"]) == 1:
            body = json.loads(req.content)
            assert body["source_branch"] == "fabrique/FAB-1" and body["target_branch"] == "main" and body["remove_source_branch"]
            return httpx.Response(201, json={"iid": 3})
        if req.method == "POST":
            return httpx.Response(409, json={"message": ["Another open merge request already exists"]})
        if req.method == "GET":
            return httpx.Response(200, json=[{"iid": 3}])
        return httpx.Response(200, json={"iid": 3})
    g = gitlab(handler)
    assert g.open_pr("grp/proj", "fabrique/FAB-1", "main", "t", "b") == 3
    assert g.open_pr("grp/proj", "fabrique/FAB-1", "main", "t2", "b2") == 3
    assert ("PUT", "/api/v4/projects/grp%2Fproj/merge_requests/3") in calls       # mise à jour, pas de doublon


def test_pr_is_normalised_like_forgejo():
    g = gitlab(lambda r: httpx.Response(200, json={"iid": 3, "sha": "abc123", "state": "merged", "merge_commit_sha": None,
                                                   "squash_commit_sha": "def456"}))
    assert g.pr("grp/proj", 3) == {"head": {"sha": "abc123"}, "state": "merged", "merge_commit_sha": "def456"}


def test_decision_comment_is_created_once_then_updated():
    notes, seen = [], []

    # un autre compte a déjà posté un commentaire portant le marqueur : le runner ne doit jamais le modifier
    notes.append({"id": 5, "body": forge.GitLab.MARK + "\nfaux", "author": {"username": "mallory"}})

    def handler(req):
        if req.url.path.endswith("/user"):
            return httpx.Response(200, json={"username": "fabrique-bot"})
        seen.append((req.method, req.url.path))
        if req.method == "GET":
            return httpx.Response(200, json=notes)
        if req.method == "POST":
            notes.append({"id": 9, "body": json.loads(req.content)["body"], "author": {"username": "fabrique-bot"}})
            return httpx.Response(201, json=notes[-1])
        assert req.url.path.endswith("/notes/9")
        notes[1]["body"] = json.loads(req.content)["body"]
        return httpx.Response(200, json=notes[1])
    g = gitlab(handler)
    g.upsert_comment("grp/proj", 3, "v1")
    g.upsert_comment("grp/proj", 3, "v2")
    assert len(notes) == 2 and notes[1]["body"].endswith("v2") and forge.GitLab.MARK in notes[1]["body"]
    assert notes[0]["body"].endswith("faux")
    assert [m for m, _ in seen].count("POST") == 1 and [m for m, _ in seen].count("PUT") == 1


def test_merge_is_pinned_to_sha_and_retries_while_mergeability_is_computed():
    tries = []

    def handler(req):
        body = json.loads(req.content)
        tries.append(body)
        if len(tries) == 1:
            return httpx.Response(405, json={"message": "Method Not Allowed"})
        return httpx.Response(200, json={"merge_commit_sha": "m1", "squash_commit_sha": "s1"})
    assert gitlab(handler).merge("grp/proj", 3, "abc123", "titre", wait=0) == "m1"
    assert all(t["sha"] == "abc123" and t["squash"] for t in tries) and len(tries) == 2
    gitlab(handler).merge("grp/proj", 3, "abc123", "titre", wait=0, message="Fabrique-Job: FAB-1")
    assert tries[-1]["squash_commit_message"] == "titre\n\nFabrique-Job: FAB-1"         # ligne de rattachement au job


def test_merge_refused_when_head_moved():
    with pytest.raises(RuntimeError, match="409"):
        gitlab(lambda r: httpx.Response(409, json={"message": "SHA does not match HEAD of source branch"})).merge("g/p", 3, "x", "t", wait=0)


def test_make_by_provider():
    assert isinstance(forge.make("gitlab"), forge.GitLab) and isinstance(forge.make("forgejo"), forge.Forgejo)
    assert isinstance(forge.make("gitea"), forge.Forgejo)                                    # Gitea : même API que Forgejo
    with pytest.raises(RuntimeError):
        forge.make("svn")


def github(handler):
    return forge.GitHub(base="https://gh.example", api="https://api.gh.example", token="tok", transport=httpx.MockTransport(handler))


def test_github_git_env_and_urls():
    g = github(lambda r: httpx.Response(500))
    env = g.git_env()
    assert g.clone_url("moi/essai") == "https://gh.example/moi/essai.git"
    assert base64.b64decode(env["GIT_CONFIG_VALUE_0"].split()[-1]).decode() == "x-access-token:tok"
    assert g.pr_url("moi/essai", 4) == "https://gh.example/moi/essai/pull/4"


def test_github_open_pr_existing_is_updated():
    seen = []

    def handler(req):
        seen.append((req.method, req.url.path, dict(req.url.params)))
        assert req.headers["Authorization"] == "Bearer tok"
        if req.method == "POST":
            return httpx.Response(422, json={"message": "Validation Failed", "errors": [{"message": "A pull request already exists for moi:fabrique/FAB-1."}]})
        if req.method == "GET":
            return httpx.Response(200, json=[{"number": 5}])
        return httpx.Response(200, json={"number": 5})
    assert github(handler).open_pr("moi/essai", "fabrique/FAB-1", "main", "t", "b") == 5
    assert ("GET", "/repos/moi/essai/pulls", {"state": "open", "head": "moi:fabrique/FAB-1"}) in seen
    assert any(m == "PATCH" for m, _, _ in seen)


def test_github_merge_pinned_and_branch_deleted():
    seen = []

    def handler(req):
        seen.append((req.method, req.url.path))
        if req.method == "PUT":
            body = json.loads(req.content)
            assert body == {"sha": "abc", "merge_method": "squash", "commit_title": "t"}
            return httpx.Response(200, json={"sha": "m1", "merged": True})
        if req.method == "GET":
            return httpx.Response(200, json={"head": {"sha": "abc", "ref": "fabrique/FAB-1"}, "merged": True, "merge_commit_sha": "m1"})
        return httpx.Response(204)
    assert github(handler).merge("moi/essai", 5, "abc", "t", wait=0) == "m1"
    assert ("DELETE", "/repos/moi/essai/git/refs/heads/fabrique/FAB-1") in seen


def test_github_merge_refused_when_head_moved():
    with pytest.raises(RuntimeError, match="409"):
        github(lambda r: httpx.Response(409, json={"message": "Head branch was modified."})).merge("moi/essai", 5, "abc", "t", wait=0)


def test_make_github():
    assert isinstance(forge.make("github"), forge.GitHub)


def test_create_repo_per_forge():
    seen = []

    def h(req):
        seen.append((req.method, req.url.path, json.loads(req.content) if req.content else None))
        if req.url.path == "/api/v4/groups/fabrique":
            return httpx.Response(200, json={"id": 39})
        if req.url.path == "/user":
            return httpx.Response(200, json={"login": "moi"})
        return httpx.Response(201, json={})
    fj = forge.Forgejo(base="https://fj.example", token="t", transport=httpx.MockTransport(h))
    assert fj.create_repo("fabrique", "api", "d") == "créé"
    assert seen[-1][1] == "/api/v1/orgs/fabrique/repos" and seen[-1][2]["private"] and not seen[-1][2]["auto_init"]
    assert gitlab(h).create_repo("fabrique", "api") == "créé" and seen[-1][2]["namespace_id"] == 39 and seen[-1][2]["visibility"] == "private"
    assert github(h).create_repo("moi", "api") == "créé" and seen[-1][1] == "/user/repos"
    assert github(h).create_repo("une-org", "api") == "créé" and seen[-1][1] == "/orgs/une-org/repos"
    assert forge.Forgejo(base="https://fj.example", token="t", transport=httpx.MockTransport(lambda r: httpx.Response(409))).create_repo("o", "a") == "existe"
