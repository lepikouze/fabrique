"""Languages (0.8.0). English is the source language: templates and code are written in English, a text Fabrique words
itself is marked with M("…", name=value), and each reader sees it in their own language from the catalogues. The job
language (what agents and Fabrique write for people) is a separate setting."""
import json, os, re, sys
import pytest
from jinja2 import Environment, FileSystemLoader
from markupsafe import Markup
from app import db, i18n, jobs, main, notify, work
from app.i18n import M
from .conftest import ADMIN, web

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, os.path.join(ROOT, "tools"))
import i18n_extract  # noqa: E402

OTHERS = [lang for lang in i18n.LANGS if lang != i18n.DEFAULT]
HOLE = re.compile(r"\{(\w+)\}")
TAG = re.compile(r"</?[a-zA-Z][\w-]*")


@pytest.fixture
def french():
    token = i18n.use("fr")
    yield
    i18n._lang.reset(token)


def messages(lang):
    return json.load(open(os.path.join(ROOT, "app", "locales", f"{lang}.json"), encoding="utf-8"))["messages"]


def forms(value):
    return list(value.values()) if isinstance(value, dict) else [value]


# ---------- the catalogues cover everything ----------

@pytest.mark.parametrize("lang", OTHERS)
def test_every_template_text_has_a_translation(lang):
    env = Environment(loader=FileSystemLoader(os.path.join(ROOT, "app", "templates")), keep_trailing_newline=True)
    fixed, _ = i18n.template_texts(env)
    known = messages(lang)
    missing = [f"{name} | {text}" for text, name in fixed.items() if text not in known and f"{name}::{text}" not in known]
    assert fixed and not missing, "template texts without a translation:\n" + "\n".join(missing)


@pytest.mark.parametrize("lang", OTHERS)
def test_every_text_worded_by_the_code_is_marked_and_translated(lang):
    known = messages(lang)
    texts = i18n_extract.code_texts(ROOT)
    missing = [f"{where} | {text}" for text, where in texts.items() if text not in known]
    assert len(texts) > 500 and not missing, "texts of the code without a translation:\n" + "\n".join(missing)
    runner = json.load(open(os.path.join(ROOT, "runner", "locales", f"{lang}.json"), encoding="utf-8"))
    written = i18n_extract.code_texts(ROOT, kind="W")
    assert written and not [t for t in written if t not in runner], sorted(t for t in written if t not in runner)


def test_no_french_is_left_in_the_code():
    """French in a string literal is a text that escaped the catalogues (the tests' ignore list holds the known rest:
    server logs and comments are the next step)."""
    ignore = set(json.load(open(os.path.join(HERE, "i18n_ignore.json"), encoding="utf-8")))
    left = [f"{where} | {text[:80]}" for where, text in i18n_extract.french_left(ROOT) if text not in ignore]
    assert not left, "French left in the code:\n" + "\n".join(left)


@pytest.mark.parametrize("lang", OTHERS)
def test_a_translation_keeps_the_values_and_the_markup(lang):
    bad = []
    for key, value in messages(lang).items():
        src = key.split("::", 1)[-1]
        for form in forms(value):
            if sorted(set(HOLE.findall(src.replace("{{", "").replace("}}", "")))) != sorted(set(HOLE.findall(form.replace("{{", "").replace("}}", "")))):
                bad.append("values: " + key)
            if sorted(TAG.findall(src)) != sorted(TAG.findall(form)):
                bad.append("markup: " + key)
            if not form.strip():
                bad.append("empty: " + key)
    assert not bad, "\n".join(bad[:40])


def test_scripts_take_their_texts_from_the_catalogue():
    js = open(os.path.join(ROOT, "app", "static", "app.js"), encoding="utf-8").read()
    used = {json.loads(m) for m in re.findall(r'\bT\(("(?:[^"\\]|\\.)*")\)', js)}
    declared = set(i18n.catalogue("en").meta["js"])
    assert used and used <= declared, used - declared
    for lang in OTHERS:
        assert set(i18n.js_catalogue(lang)) == declared and all(k in messages(lang) for k in declared)
    assert i18n.js_catalogue("en") == {}


# ---------- a text is a fact, worded for each reader ----------

def test_a_message_carries_its_template_and_values(french):
    m = M("{ref} archived.", ref="FAB-12")
    assert m == "FAB-12 archived." and isinstance(m, str) and m.tid == "{ref} archived." and m.params == {"ref": "FAB-12"}
    assert i18n.tr(m) == "FAB-12 archivé." and i18n.tr(m, "en") == "FAB-12 archived."
    assert i18n.tr(M("Merged")) == "Mergé" and i18n.tr(M("Unknown text {x}", x=1)) == "Unknown text 1"
    assert json.dumps({"m": m}) == '{"m": "FAB-12 archived."}'                 # a plain string for whoever does not know


def test_a_plain_sentence_is_recognised_too(french):
    """A sentence that lost its values on the way (stored as text, sent by a runner) is matched against the templates."""
    assert i18n.tr("FAB-12 archived.") == "FAB-12 archivé." and i18n.tr("Merged") == "Mergé"
    assert i18n.tr("Texte libre d'un agent") == "Texte libre d'un agent" and i18n.tr("") == ""
    assert i18n.tr("  Merged\n") == "  Mergé\n"
    assert i18n.tr("Merged", "en") == "Merged"


def test_plural_and_formats(french):
    assert i18n.plural(1, "{n} active", "{n} active") == "1 actif" and i18n.plural(3, "{n} active", "{n} active") == "3 actifs"
    assert i18n.money(1284.6) != "" and i18n.decimal(1.5, 1) == "1,5"
    i18n.use("en")
    assert i18n.plural(3, "{n} active", "{n} active") == "3 active" and i18n.decimal(1.5, 1) == "1.5"


def test_language_of_a_request():
    assert i18n.pick("fr", "en") == "fr" and i18n.pick("", "fr-FR,fr;q=0.9,en;q=0.8") == "fr" and i18n.pick("", "de, en;q=0.5") == "en"
    assert i18n.pick("", "de") == "en" and i18n.pick("xx", "") == "en" and i18n.pick("", "", "fr") == "fr"


def test_template_texts_values_and_attributes(french):
    env = Environment(autoescape=True)
    i18n.install(env)
    t = env.from_string('<p title="Close">Merged <b>{{ s }}</b></p><input name="s" value="{{ s }}" placeholder="{{ s }}"><a class="{{ s }}" href="/x?s={{ s }}">x</a>')
    out = t.render(s="Merged")
    assert '<p title="Fermer">Mergé <b>Mergé</b></p>' in out                  # fixed text, text attribute, displayed value
    assert 'value="Merged"' in out and 'class="Merged"' in out and "/x?s=Merged" in out and 'placeholder="Mergé"' in out
    i18n.use("en")
    assert '<p title="Close">Merged <b>Merged</b></p>' in t.render(s="Merged")


def test_a_value_stays_inside_its_sentence(french, monkeypatch):
    """A simple value in the middle of a sentence is part of the unit: the sentence is translated whole, and a language
    may move the values. Values are escaped; a value in an address or a class is never touched."""
    env = Environment(autoescape=True)
    i18n.install(env)
    src = ('<p>Follow-up to the study {{ pre.ref }}</p><button title="Remove {{ name }}?">x</button>'
           '<p>{{ a }} of {{ b }}</p><a href="/jobs/{{ pre.ref }}">{{ pre.ref }}</a>')
    units = [t.value for f, p, t in zip(*[list(i18n.Texts(env).filter_stream(env.lexer.tokenize(src, "x.html")))[i:] for i in range(3)])
             if f.type == "name" and f.value == "_tf" and t.type == "string"]
    assert units == ["Follow-up to the study {ref}", "Remove {name}?", "{a} of {b}"]          # named after their expression
    monkeypatch.setitem(i18n.catalogue("fr").exact, "{a} of {b}", "{b} parmi {a}")
    t = env.from_string(src)
    out = t.render(pre={"ref": "FAB-<3>"}, name='"x"', a=1, b=2)
    assert "<p>Suite de l'étude FAB-&lt;3&gt;</p>" in out.replace("&#39;", "'") and 'title="Retirer &#34;x&#34; ?"' in out
    assert "<p>2 parmi 1</p>" in out and 'href="/jobs/FAB-&lt;3&gt;"' in out
    i18n.use("en")
    assert "<p>Follow-up to the study FAB-&lt;3&gt;</p>" in t.render(pre={"ref": "FAB-<3>"}, name="x", a=1, b=2)


def test_a_context_tells_two_uses_of_the_same_word_apart(french):
    assert i18n.tr(M("done")) == "terminée" and i18n.tr(M("done", _ctx="job")) == "terminé"
    assert i18n.tr(M("Refused")) == "Refusée" and i18n.tr(M("Refused", _ctx="job")) == "Refusé"
    assert i18n.tr(M("done", _ctx="nowhere")) == "terminée"                     # unknown context: the general translation
    stored = i18n.revive(i18n.pack(M("none", _ctx="model")))                    # the context survives storage
    assert i18n.tr(stored) == "aucun" and str(stored) == "none"
    i18n.use("en")
    assert i18n.tr(M("done", _ctx="job")) == "done"


def test_scripts_styles_and_comments_are_left_alone(french):
    env = Environment(autoescape=True)
    i18n.install(env)
    out = env.from_string("<script>var a = 'Merged';</script><style>.Merged{}</style><!-- Merged --><p>Merged</p>").render()
    assert "var a = 'Merged'" in out and ".Merged{}" in out and "<!-- Merged -->" in out and "<p>Mergé</p>" in out


# ---------- interface language: the browser, then the person's choice ----------

def test_pages_follow_the_browser_then_the_choice(repo):
    cl = web()
    en = cl.get("/jobs").text
    assert '<html lang="en"' in en and "New request" in en                              # neither choice nor browser: English
    fr = cl.get("/jobs", headers={"accept-language": "fr-FR,fr;q=0.9"}).text
    assert '<html lang="fr"' in fr and "Nouvelle demande" in fr and "New request" not in fr
    # the choice is one line of the menu, in every mode: a list with "Automatic" first, then each language under its own name
    assert 'action="/account/lang"' in en and '<select id="um-lang" name="lang" data-autosubmit>' in en
    assert '<option value="" selected>Automatic (browser)</option>' in en
    assert '<option value="fr" lang="fr" >Français</option>' in en and '<option value="en" lang="en" >English</option>' in en
    r = cl.post("/account/lang", data={"lang": "fr"}, headers={"origin": "http://testserver", "referer": "http://testserver/repos"})
    assert r.status_code == 303 and r.headers["location"] == "/repos" and "fabrique_lang=fr" in r.headers["set-cookie"]
    assert "Nouvelle demande" in cl.get("/jobs", headers={"accept-language": "en"}).text   # the choice comes before the browser
    cl.post("/account/lang", data={"lang": ""}, headers={"origin": "http://testserver"})
    cl.cookies.delete("fabrique_lang")
    assert "New request" in cl.get("/jobs").text


def test_a_language_is_a_catalogue_file(tmp_path, monkeypatch):
    """Adding a language only takes its catalogue: the list on offer is read from app/locales."""
    import json, shutil
    folder = tmp_path / "locales"
    shutil.copytree(os.path.join(os.path.dirname(i18n.__file__), "locales"), folder)
    es = json.load(open(folder / "fr.json", encoding="utf-8"))
    es["meta"].update(name="Español", english_name="Spanish")
    json.dump(es, open(folder / "es.json", "w", encoding="utf-8"), ensure_ascii=False)
    (folder / "broken.json").write_text("{not json")
    monkeypatch.setattr(i18n, "HERE", str(tmp_path))
    names, english = i18n._discover()
    assert list(names.items()) == [("en", "English"), ("es", "Español"), ("fr", "Français")]      # ordered by their own name
    assert english == {"en": "English", "es": "Spanish", "fr": "French"}
    assert set(i18n.LANGS) == {"en", "fr"} and i18n.ENGLISH_NAMES["fr"] == "French"                # what ships today


def test_instance_default_when_nothing_decides(repo, monkeypatch):
    monkeypatch.setenv("FABRIQUE_LANG", "fr")
    cl = web()
    assert "Nouvelle demande" in cl.get("/jobs").text
    assert "New request" in cl.get("/jobs", headers={"accept-language": "en"}).text


def test_the_choice_is_stored_on_the_account_and_follows_the_person(repo, monkeypatch):
    import time
    from sqlalchemy import insert, select
    from app import auth
    with db.tx() as c:
        c.execute(insert(db.users).values(username="alice", name="Alice", role="admin", created_at=time.time(), groups=[]))
    monkeypatch.setattr(auth, "who", lambda request: {"user": "alice", "name": "Alice", "role": "admin", "local": False})
    first = web()
    assert first.post("/account/lang", data={"lang": "fr"}, headers={"origin": "http://testserver"}).status_code == 303
    with db.tx() as c:
        assert db.one(c, select(db.users.c.prefs).where(db.users.c.username == "alice"))["prefs"]["lang"] == "fr"
    other = web()                                                      # another machine, English browser, no cookie
    r = other.get("/jobs", headers={"accept-language": "en"})
    assert "Nouvelle demande" in r.text and "fabrique_lang=fr" in r.headers.get("set-cookie", "")
    first.post("/account/lang", data={"lang": ""}, headers={"origin": "http://testserver"})
    with db.tx() as c:
        assert "lang" not in (db.one(c, select(db.users.c.prefs).where(db.users.c.username == "alice"))["prefs"] or {})


# ---------- job language: what agents and Fabrique write for people ----------

def test_job_language_setting(repo):
    cl = web()
    assert cl.post("/settings/lang", data={"jobs": "xx"}, headers={"origin": "http://testserver"}).headers["location"] == "/settings?tab=lang"
    assert cl.post("/settings/lang", data={"jobs": "fr"}, headers={"origin": "http://testserver"}).status_code == 303
    with db.tx() as c:
        assert db.get_setting(c, "languages")[0] == {"jobs": "fr"}
        c.execute(db.settings.delete().where(db.settings.c.key == "languages"))
        assert i18n.job_lang(c) == "en"                                # a new installation: English


def test_job_language_goes_to_the_runner_and_to_notifications(repo):
    from sqlalchemy import select, update
    with db.tx() as c:
        r = db.one(c, select(db.repos).where(db.repos.c.id == repo))
        assert i18n.job_lang(c, r) == "en"
        db.set_setting(c, "languages", {"jobs": "fr"}, "test")
        assert i18n.job_lang(c, r) == "fr"
        c.execute(update(db.repos).where(db.repos.c.id == repo).values(settings=dict(r["settings"], lang="en")))
        assert i18n.job_lang(c, db.one(c, select(db.repos).where(db.repos.c.id == repo))) == "en"       # the repo wins
        c.execute(update(db.repos).where(db.repos.c.id == repo).values(settings=dict(r["settings"], lang="")))
        p = notify.written(c, notify.message("ready", {"ref": "FAB-1", "title": "Exporter"}, None, "Ready for review."))
        assert p["lang"] == "fr" and p["title"] == "FAB-1 : PR prête à relire" and p["text"] == "Exporter\nPrête à relire."
        db.set_setting(c, "languages", {"jobs": "en"}, "test")
        p = notify.written(c, notify.message("ready", {"ref": "FAB-1", "title": "Exporter"}, None, "Ready for review."))
    assert p["title"] == "FAB-1: PR ready for review" and p["lang"] == "en" and p["text"] == "Exporter\nReady for review."


def test_runner_writes_on_the_git_host_in_the_job_language():
    sys.path.insert(0, ROOT)
    from runner import texts
    class Ctx:
        repo = {"lang": "fr"}
    assert texts.W(Ctx, "**Where to start**: ") == "**Par où commencer** : " and texts.W(Ctx, "unknown text") == "unknown text"
    Ctx.repo = {"lang": "en"}
    assert texts.W(Ctx, "**Where to start**: ") == "**Where to start**: "
    Ctx.repo = {}
    assert texts.W(Ctx, "**Where to start**: ") == "**Where to start**: "


# ---------- what Fabrique says of a job follows each reader ----------

def test_the_same_job_is_read_in_each_reader_language(repo):
    ref = jobs.create(ADMIN, "Exporter", "Besoin", "Critère", repo)
    cl = web()
    en = cl.get(f"/jobs/{ref}").text
    fr = cl.get(f"/jobs/{ref}", headers={"accept-language": "fr"}).text
    assert "Request filed by" in en and "Demande déposée par" not in en
    assert "Demande déposée par" in fr and "Request filed by" not in fr
    assert "Exporter" in en and "Exporter" in fr and "Besoin" in fr                        # what a person wrote is never translated


def test_what_fabrique_says_is_stored_as_a_fact_not_as_a_sentence(repo):
    """Events, status, audit: the database keeps the template and its values; nothing is frozen in a language."""
    from sqlalchemy import select, text
    ref = jobs.create(ADMIN, "Exporter", "Besoin", "Critère", repo)
    with db.tx() as c:
        raw = [r[0] for r in c.execute(text("select message from events"))] + [r[0] for r in c.execute(text("select status_text from jobs"))] \
            + [r[0] for r in c.execute(text("select action from audit"))]
        assert raw and all(r.startswith(i18n.MARK) for r in raw if r), [r for r in raw if r and not r.startswith(i18n.MARK)]
        assert not any("Demande déposée" in r for r in raw)
        fact = json.loads(next(r for r in raw if "Request filed by" in r)[1:])
        assert fact["m"] == "Request filed by {name}" and fact["p"] == {"name": ADMIN["name"]}
        ev = db.rows(c, select(db.events))[0]["message"]
    assert isinstance(ev, i18n.Msg) and i18n.tr(ev, "fr").startswith("Demande déposée par") and str(ev).startswith("Request filed by")


def test_a_fact_survives_being_joined_sent_and_stored():
    a = M("{ref} archived.", ref="FAB-3") + " " + M("Merged")
    assert a == "FAB-3 archived. Merged" and i18n.tr(a, "fr") == "FAB-3 archivé. Mergé"
    wire = json.loads(json.dumps(i18n.pack_tree({"status": a, "n": 2, "list": [M("Merged"), "data"]})))     # what a runner sends
    back = i18n.revive_tree(wire)
    assert back["status"] == a and i18n.tr(back["status"], "fr") == "FAB-3 archivé. Mergé" and back["n"] == 2
    assert i18n.tr(back["list"][0], "fr") == "Mergé" and back["list"][1] == "data" and not isinstance(back["list"][1], i18n.Msg)
    assert i18n.revive(i18n.MARK + "not json") == "not json" and i18n.pack("plain") == "plain"
    sys.path.insert(0, ROOT)
    from runner import texts
    sent = texts.pack_tree({"message": texts.M("Network egress: {na} allowed, none refused", na=3)})    # the runner's own class, same format
    got = i18n.revive_tree(json.loads(json.dumps(sent)))["message"]
    assert got == "Network egress: 3 allowed, none refused" and i18n.tr(got, "fr").startswith("Sorties réseau")


# ---------- machine translation of a job's texts, on request ----------

def _job_in_french(repo, monkeypatch):
    """A finished job written in French (the tests' job language), with an agent's opinion and a reviewer's points."""
    import time
    from sqlalchemy import insert, select, update
    with db.tx() as c:
        db.set_setting(c, "languages", {"jobs": "fr"}, "test")
    ref = jobs.create(ADMIN, "Exporter", "Besoin", "Critère", repo)
    with db.tx() as c:
        jid = db.one(c, select(db.jobs.c.id).where(db.jobs.c.ref == ref))["id"]
        c.execute(update(db.intents).values(state="done"))
        c.execute(insert(db.decisions).values(job_id=jid, policy="off", at=time.time(), confident=True, limits=[], recommendation="",
                                              interpretation="La demande vise l'export des liens.", assumptions=["Le format est CSV."],
                                              verdict="Les tests passent.", notes=""))
        c.execute(insert(db.reviews).values(job_id=jid, sha="a" * 40, role="code", verdict="ok", summary="Rien à signaler sur ce diff.",
                                            points=["notebook/export.py : la fonction est testée."], model="m", at=time.time()))
        db.set_setting(c, "translation", {"model": "gemma-4-26b-a4b"}, "test")
    return ref


def _fake_model(seen, fail=False):
    import httpx

    def handler(req):
        body = json.loads(req.content)
        seen.append(body)
        if fail:
            return httpx.Response(500)
        text = body["messages"][1]["content"].replace("<text>\n", "").replace("\n</text>", "")
        return httpx.Response(200, json={"choices": [{"message": {"content": "EN: " + text}}]})
    return httpx.MockTransport(handler)


def test_translate_button_only_when_the_reader_reads_another_language(repo, monkeypatch):
    ref = _job_in_french(repo, monkeypatch)
    cl = web()
    assert "/translate" not in cl.get(f"/jobs/{ref}", headers={"accept-language": "fr"}).text                             # French reader, French job: nothing to offer
    page = cl.get(f"/jobs/{ref}", headers={"accept-language": "en"}).text
    assert f'action="/jobs/{ref}/translate"' in page and "Translate" in page and "data-mt" not in page   # offered, never automatic
    with db.tx() as c:
        db.set_setting(c, "translation", {"model": ""}, "test")
    assert "/translate" not in cl.get(f"/jobs/{ref}", headers={"accept-language": "en"}).text   # no model chosen: no button


def test_translation_is_stored_shown_as_such_and_only_prose_is_sent(repo, monkeypatch):
    from sqlalchemy import select, update
    from app import providers, translate
    ref = _job_in_french(repo, monkeypatch)
    seen = []
    monkeypatch.setattr(providers, "TRANSPORT", _fake_model(seen))
    cl = web()
    r = cl.post(f"/jobs/{ref}/translate", headers={"origin": "http://testserver", "accept-language": "en", "referer": f"http://testserver/jobs/{ref}"})
    assert r.status_code == 303 and r.headers["location"] == f"/jobs/{ref}"
    sent = [b["messages"][1]["content"] for b in seen]
    assert len(sent) == 5 and any("La demande vise l'export" in s for s in sent)        # interpretation, assumption, verdict, review summary and point
    assert all("into English" in b["messages"][0]["content"] and "DATA" in b["messages"][0]["content"] for b in seen)
    assert not any("Besoin" in s or "Critère" in s or "Exporter" in s for s in sent)    # what a person wrote is not sent, nor anything Fabrique words itself
    assert all(b["chat_template_kwargs"] == {"enable_thinking": False} for b in seen)
    page = cl.get(f"/jobs/{ref}", headers={"accept-language": "en"}).text
    assert "Machine translation" in page and "EN: La demande vise l&#39;export des liens." in page
    assert 'data-mo hidden>La demande vise l&#39;export des liens.' in page             # the original is in the page, one click away
    assert "EN: " not in cl.get(f"/jobs/{ref}?orig=1", headers={"accept-language": "en"}).text
    assert "EN: " not in cl.get(f"/jobs/{ref}", headers={"accept-language": "fr"}).text                                    # a French reader keeps the original
    cl.post(f"/jobs/{ref}/translate", headers={"origin": "http://testserver", "accept-language": "en"})
    assert len(seen) == 5                                                               # already stored: the model is not asked again
    with db.tx() as c:
        jid = db.one(c, select(db.jobs.c.id).where(db.jobs.c.ref == ref))["id"]
        c.execute(update(db.decisions).where(db.decisions.c.job_id == jid).values(verdict="Les tests passent, sauf un."))
        logs = [a for a in db.rows(c, select(db.audit)) if a["action"] == "Job translated"]
    assert logs and "5/5" in logs[0]["detail"] and "La demande" not in logs[0]["detail"]   # audited, without the texts
    cl.post(f"/jobs/{ref}/translate", headers={"origin": "http://testserver", "accept-language": "en"})
    assert len(seen) == 6 and "sauf un" in seen[-1]["messages"][1]["content"]           # a text that changed is translated again


def test_translation_failure_says_so_and_keeps_nothing(repo, monkeypatch):
    from sqlalchemy import select
    from app import providers
    ref = _job_in_french(repo, monkeypatch)
    monkeypatch.setattr(providers, "TRANSPORT", _fake_model([], fail=True))
    cl = web()
    r = cl.post(f"/jobs/{ref}/translate", headers={"origin": "http://testserver", "accept-language": "en"})
    assert r.status_code == 303 and "fabrique_flash" in r.headers.get("set-cookie", "")
    with db.tx() as c:
        assert db.rows(c, select(db.translations)) == []


def test_long_texts_are_cut_at_paragraphs_and_bounded():
    from app import translate
    text = "\n\n".join("Paragraphe %d. " % i + "x" * 900 for i in range(12))
    pieces = translate._pieces(text)
    assert len(pieces) > 1 and all(len(p) <= translate.MAX_TEXT for p in pieces) and "\n\n".join(pieces) == text
    assert translate._pieces("court") == ["court"]
    j = {"decision": {"interpretation": "Texte de l'agent", "assumptions": [], "verdict": "", "notes": "", "recommendation": "The agent recommends merging."},
         "questions": [], "report": "", "reviews": [], "timeline": [{"summary": "Texte de l'agent"}]}
    assert translate.prose(j) == ["Texte de l'agent"]                                   # no duplicate, and nothing Fabrique words itself
