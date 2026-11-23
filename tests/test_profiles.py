"""Model profiles (0.7.0): one model and one effort per role, priority order, Settings, what the runner receives,
the gateway that chooses the right provider and the reviewers who use their model."""
import pytest
from sqlalchemy import select, update
from app import db, jobs, profiles, work
from .conftest import ADMIN, REQ, web

LOCAL, OPUS, SONNET = "qwen3.8-27b", "claude-opus-5-5", "claude-sonnet-5-5"


def roles(agent="", review="", study="", onboarding="", **effort):
    """Each role names its model: a role not specified here takes that of the agent (written in full)."""
    return {k: {"model": v or agent, "effort": effort.get(k, profiles.INHERIT)}
            for k, v in (("agent", agent), ("review", review), ("study", study), ("onboarding", onboarding))}


def add(pid, name="P", **kw):
    with db.tx() as c:
        profiles.save(c, "alice", pid, name, roles(**kw), new=True)


def resolve(rs=None, source="form", model="", profile=""):
    with db.tx() as c:
        return profiles.resolve(c, rs or {}, source, model, profile)


def set_global(profile):
    with db.tx() as c:
        g, _ = db.get_setting(c, "agents")
        db.set_setting(c, "agents", {**g, "profile": profile}, "test")


def test_without_profile_nothing_changes():
    assert resolve() == (OPUS, {"profile": "", "effort": "provider", "review_model": "", "review_effort": "provider"})
    assert resolve({"model": LOCAL})[0] == LOCAL                                     # repo model, as before


def test_priority_request_then_profile_then_repo_then_global():
    add("mixte", "Mixte", agent=LOCAL, review=SONNET, study=OPUS)
    add("autre", "Autre", agent=SONNET)
    set_global("autre")
    assert resolve()[0] == SONNET                                                    # profil global
    m, x = resolve({"model": "gpt-oss-120b", "profile": "mixte"})
    assert m == LOCAL and x["review_model"] == SONNET and x["profile"] == "Mixte"    # repo profile > repo model
    assert resolve({"profile": "mixte"}, profile="autre")[0] == SONNET               # request profile > repo profile
    m, x = resolve({"profile": "mixte"}, model=OPUS)
    assert m == OPUS and x["review_model"] == "" and x["profile"] == ""           # a chosen model: only one for everything
    assert resolve({"profile": "mixte"}, source="study")[0] == OPUS                  # Studies role
    assert resolve({"profile": "mixte"}, source="onboarding")[0] == LOCAL            # onboarding vide : comme l'agent
    m, x = resolve({"model": LOCAL})
    assert m == LOCAL and x["profile"] == ""                                         # repo model > global profile                 # empty agent: repo model


def test_effort_comes_from_the_provider_only():
    with db.tx() as c:                                                           # effort of a previous profile: ignored
        profiles.save(c, "alice", "p", "P", {k: {"model": LOCAL, "effort": "low"} for k in profiles.ROLES})
        assert all(r["effort"] == "provider" for r in profiles.get(c, "p")["roles"].values())
    _, x = resolve({"profile": "p"})
    assert x["effort"] == "provider" and x["review_effort"] == "provider"


def test_profile_id_comes_from_its_name():
    with db.tx() as c:
        assert profiles.slug("Mixed: local and Claude") == "mixed-local-and-claude" and profiles.slug("Été", {"ete"}) == "ete-2"
        profiles.save(c, "alice", "", "All local", roles(agent=LOCAL), new=True)
        assert profiles.get(c, "all-local")["name"] == "All local"
        with pytest.raises(profiles.Invalid, match="is already called"):
            profiles.save(c, "alice", "", "all LOCAL", roles(agent=LOCAL), new=True)
        profiles.save(c, "alice", "all-local", "Local seulement", roles(agent=SONNET))   # renamed: same identifier
        assert [p["id"] for p in profiles.listing(c)] == ["all-local"]


def test_profile_validation():
    with db.tx() as c, pytest.raises(profiles.Invalid, match="unknown model"):
        profiles.save(c, "a", "x", "X", roles(agent="inconnu"))
    with db.tx() as c, pytest.raises(profiles.Invalid, match="each role"):
        profiles.save(c, "a", "x", "X", {"agent": {"model": LOCAL}, "review": {"model": SONNET}, "study": {"model": ""}, "onboarding": {"model": ""}})
    with db.tx() as c, pytest.raises(profiles.Invalid, match="name"):
        profiles.save(c, "a", "", "  ", roles(agent=LOCAL))
    add("x", agent=LOCAL)
    with db.tx() as c, pytest.raises(profiles.Invalid, match="already exists"):
        profiles.save(c, "a", "x", "X", roles(agent=LOCAL), new=True)


def test_settings_page_create_edit_default_and_remove(repo):
    cl = web()
    data = {"action": "save", "pid": "mixte", "name": "Mixte", "model:agent": LOCAL, "effort:agent": "low",
            "model:review": SONNET, "effort:review": "provider", "model:study": OPUS, "effort:study": "provider",
            "model:onboarding": LOCAL, "effort:onboarding": "provider"}
    r = cl.post("/settings/profiles", data=data)
    assert "Profile saved" in cl.get(r.headers["location"]).text
    page = cl.get("/settings?tab=agents").text
    assert "Mixte" in page and "Claude Opus 5.5" in page and "Effort" not in page.split('id="profiles"')[1].split("</section>")[0]
    assert "is already called" in cl.get(cl.post("/settings/profiles", data=data).headers["location"]).text
    assert "Profile saved" in cl.get(cl.post("/settings/profiles", data={**data, "edit": "1", "name": "Mixte 2"}).headers["location"]).text
    assert 'value="mixte" selected' not in cl.get("/settings?tab=agents").text
    cl.post("/settings/agents", data={"harness": "claude-code", "model": OPUS, "profile": "mixte"})
    cl.post(f"/repos/{repo}", data={"merge_policy": "off", "profile": "mixte"})
    with db.tx() as c:
        assert db.get_setting(c, "agents")[0]["profile"] == "mixte"
        assert db.one(c, select(db.repos).where(db.repos.c.id == repo))["settings"]["profile"] == "mixte"
        assert profiles.usage(c, "mixte") == {"global": True, "repos": ["alice/essai"]}
    assert "default" in cl.get("/settings?tab=agents").text and "Mixte 2" in cl.get(f"/repos?repo={repo}").text
    assert "Unknown model profile." in cl.get(cl.post("/settings/agents", data={"harness": "claude-code", "model": OPUS, "profile": "nope"}).headers["location"]).text
    cl.post("/settings/profiles", data={"action": "remove", "pid": "mixte"})
    with db.tx() as c:                                                               # removed: everything reverts to the model
        assert db.get_setting(c, "agents")[0]["profile"] == "" and profiles.listing(c) == []
        assert "profile" not in db.one(c, select(db.repos).where(db.repos.c.id == repo))["settings"]
    assert web("reviewer").post("/settings/profiles", data=data).status_code in (302, 303)
    with db.tx() as c:
        assert profiles.listing(c) == []                                             # only an admin saves a profile


def test_job_keeps_its_models_and_the_runner_receives_them(repo):
    add("mixte", "Mixte", agent=LOCAL, review=OPUS)
    with db.tx() as c:
        c.execute(update(db.repos).where(db.repos.c.id == repo).values(settings={"merge_policy": "off", "profile": "mixte"}))
    ref = jobs.create(ADMIN, "T", "B", "", repo)
    with db.tx() as c:                                                               # profile modified afterwards: the job keeps its own
        profiles.save(c, "alice", "mixte", "Mixte", roles(agent=SONNET))
    j = jobs.get(ADMIN, ref)
    assert j["model"] == LOCAL and j["models"]["review_model"] == OPUS and j["models"]["profile"] == "Mixte"
    page = web().get(f"/jobs/{ref}/panel").text
    assert f"reviewed by <span class=\"mono\">{OPUS}</span>" in page and "profile Mixte" in page
    tok = work.create_runner("runner-test", "container", "alice")
    me = work.runner_for(tok)
    item = work.claim(me)
    assert item["model"]["id"] == LOCAL and item["review_model"]["id"] == OPUS and item["review_model"]["provider"] == "anthropic"


def test_runner_gets_no_separate_review_model_without_a_profile(repo):
    ref = jobs.create(ADMIN, "T", "B", "", repo)
    item = work.claim(work.runner_for(work.create_runner("runner-test", "container", "alice")))
    assert item["job"]["ref"] == ref and item["review_model"] is None


def test_runner_gets_the_provider_effort_not_the_profile(repo):
    with db.tx() as c:
        profiles.save(c, "alice", "rapide", "Rapide", {k: {"model": LOCAL, "effort": "low"} for k in profiles.ROLES})
    jobs.create(ADMIN, "T", "B", "", repo, profile="rapide")
    item = work.claim(work.runner_for(work.create_runner("runner-test", "container", "alice")))
    assert item["model"]["effort"] == "medium" and item["review_model"] is None        # provider setting (default medium)


def test_requester_cannot_pick_a_profile(repo):
    add("mixte", agent=LOCAL)
    ref = jobs.create(REQ, "T", "B", "", repo, profile="mixte")
    assert jobs.get(ADMIN, ref)["model"] == OPUS
    with pytest.raises(jobs.Refused, match="profile"):
        jobs.create(ADMIN, "T", "B", "", repo, profile="nope")


def test_request_form_shows_the_repo_models(repo):
    add("mixte", "Mixte", agent=LOCAL, review=SONNET)
    with db.tx() as c:
        c.execute(update(db.repos).where(db.repos.c.id == repo).values(settings={"merge_policy": "off", "profile": "mixte"}))
    page = web().get("/new").text
    assert f"<b>{LOCAL}</b>, reviewed by <b>Claude Sonnet 5.5</b> (profile Mixte)" in page and 'name="choice"' in page


def test_gateway_routes_each_request_to_the_right_model():
    from runner import gateway

    class Ctx:
        model = {"id": LOCAL, "provider": "openai", "type": "compatible", "base_url": "http://modele.test:8080/v1"}
        review_model = {"id": OPUS, "provider": "anthropic", "type": "anthropic", "key": "k"}
    ctx = Ctx()
    assert gateway.pick(ctx, OPUS) is ctx.review_model and gateway.pick(ctx, LOCAL) is ctx.model
    assert gateway.pick(ctx, "claude-haiku-5-5") is ctx.review_model                # small internal Claude Code model
    assert gateway.pick(ctx, "autre-modele") is ctx.model and gateway.pick(ctx, None) is ctx.model
    ctx.review_model = ctx.model
    assert gateway.pick(ctx, OPUS) is ctx.model                                       # a single model: never another provider


def test_reviewers_use_their_own_model(monkeypatch):
    from runner import review
    seen = []

    def ask(model, system, prompt, timeout, transport=None, think=True):
        seen.append(model["id"])
        return '{"verdict": "ok", "resume": "rien", "points": []}', 0.0, False
    monkeypatch.setattr(review, "ask", ask)
    monkeypatch.setattr(review, "prompts", lambda ctx, role, diff, how: ("Code", "sys", "p"))

    class Ctx:
        ref = "FAB-1"
        model = {"id": LOCAL, "provider": "openai", "type": "compatible", "base_url": "http://m/v1"}
        review_model = {"id": OPUS, "provider": "anthropic", "type": "anthropic", "key": "k"}
    review.run(Ctx(), "code", "diff")
    assert seen == [OPUS]


def test_onboarding_relaunch_takes_the_current_profile(repo):
    add("mixte", agent=LOCAL, onboarding=SONNET)
    with db.tx() as c:
        c.execute(update(db.repos).where(db.repos.c.id == repo).values(onboarding="failed", settings={"merge_policy": "off"}))
        ref = jobs.insert_job(c, ADMIN, "Onboarding", "R", "D", repo, source="onboarding", kind="onboard")
        c.execute(update(db.jobs).where(db.jobs.c.ref == ref).values(stage="failed"))
        c.execute(update(db.intents).values(state="failed"))
        c.execute(update(db.repos).where(db.repos.c.id == repo).values(settings={"merge_policy": "off", "profile": "mixte"}))
    assert jobs.get(ADMIN, ref)["model"] == OPUS
    assert jobs.onboard(ADMIN, repo) == ref                                          # same job, restarted
    j = jobs.get(ADMIN, ref)
    assert j["model"] == SONNET and j["models"]["profile"] == "P"


def test_one_list_model_or_profile_everywhere(repo):
    add("mixte", "Mixte", agent=LOCAL, review=SONNET)
    cl = web()
    assert profiles.parse_choice("p:mixte") == ("", "mixte") and profiles.parse_choice("m:x") == ("x", "") and profiles.parse_choice("") == ("", "")
    cl.post("/settings/agents", data={"harness": "claude-code", "choice": "p:mixte"})
    with db.tx() as c:
        assert db.get_setting(c, "agents")[0] == {"harness": "claude-code", "model": LOCAL, "profile": "mixte"}   # model = profile agent
    page = cl.get("/settings?tab=agents").text
    assert 'optgroup label="Profiles: one model per role"' in page and 'value="p:mixte"' in page and "with profile <b>Mixte</b>" in page
    assert "Example: Claude Opus 5.5" in page and "Même modèle que l’agent" not in page
    cl.post(f"/repos/{repo}", data={"merge_policy": "off", "accepts_jobs": "1", "choice": f"m:{OPUS}"})
    with db.tx() as c:
        s = db.one(c, select(db.repos).where(db.repos.c.id == repo))["settings"]
    assert s["model"] == OPUS and s["profile"] == ""                                   # one or the other, never both
    assert "Global setting (profile Mixte)" in cl.get(f"/repos?repo={repo}").text
    r = cl.post("/new", data={"title": "T", "need": "B", "repo_id": repo, "choice": "p:mixte"})
    assert r.status_code == 303
    ref = r.headers["location"].split("job=")[1]
    assert jobs.get(ADMIN, ref)["models"]["review_model"] == SONNET
    cl.post(f"/repos/{repo}", data={"merge_policy": "off", "choice": ""})
    with db.tx() as c:
        s = db.one(c, select(db.repos).where(db.repos.c.id == repo))["settings"]
    assert s["model"] == "" and s["profile"] == ""                                     # global setting
