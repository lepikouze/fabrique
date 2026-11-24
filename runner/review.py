"""Reviews: one reviewer per role, on the verified diff, without any write permission.
Roles: code and security always; architecture and infra depending on the files touched. The verdict is read in a JSON block;
an unreadable response counts as "undetermined" (never "ok".)

Two modes (REVIEW_MODE):
- "direct" (default): the runner sends ONE request to the model (instructions + request + diff) and reads the verdict. No agent
  loop, no tools: fast, even on a local model that serves one request at a time.
- "agent": Claude Code in read-only mode (Read, Glob, Grep) in the job workspace; it can open other files.
The direct mode requires a local model (OpenAI compatible API) or an Anthropic API key; otherwise, fallback to "agent"."""
try:
    from .texts import M
except ImportError:
    try:
        from texts import M
    except ImportError:
        def M(t, **p):
            return t.format(**p) if p else t
import json, logging, os, re
import httpx
from . import config
from .agent import base_env, model_env
from .messages import data

log = logging.getLogger("runner.review")
HERE = os.path.dirname(__file__)
TEMPLATE = open(os.path.join(HERE, "review.md"), encoding="utf-8").read()
# Role key (stored, sent to the dashboard) -> label shown to people, then role name and mission as given to the model.
ROLES = {
    "code": ("code", "code", "correctness: does the change do what the request wants, without breaking what exists? Edge cases, "
                             "unhandled errors, tests that test nothing, dead code."),
    "security": ("security", "security", "vulnerabilities: unvalidated input, injection, hard-coded secrets, rights that are too broad, "
                                         "exposed personal data, dubious dependencies added."),
    "architecture": ("architecture", "architecture", "structure: does the change respect the existing modules, does it create needless "
                                              "dependencies or coupling, duplication, a fragile public API?"),
    "infra": (M("infrastructure and operations"), "infrastructure and operations", "deployment: container, CI, migrations, configuration, resources, "
                                                                         "logs; what can break in production, and rollback."),
}
INFRA = re.compile(r"(^|/)(Dockerfile|docker-compose[^/]*|compose[^/]*\.ya?ml|\.forgejo/|\.gitea/|\.github/|\.gitlab-ci\.yml|k8s/|helm/|"
                   r"terraform/|.*\.tf$|migrations?/|alembic|.*\.env.*|Makefile|\.fabrique/)", re.I)
ARCHI = re.compile(r"(^|/)(requirements[^/]*\.txt|pyproject\.toml|package\.json|setup\.(py|cfg)|go\.mod|Cargo\.toml)$|__init__\.py$")


def roles_for(files):
    roles = ["code", "security"]
    new_dirs = {f.split("/")[0] for f in files if "/" in f}
    if any(ARCHI.search(f) for f in files) or len(files) > 8 or len(new_dirs) > 3:
        roles.append("architecture")
    if any(INFRA.search(f) for f in files):
        roles.append("infra")
    return roles


def parse(text):
    """Last JSON object of the response carrying a verdict: (verdict, summary, points, evidence); otherwise undetermined."""
    dec, found = json.JSONDecoder(), None
    text = text or ""
    for i, ch in enumerate(text):
        if ch == "{":
            try:
                d, _ = dec.raw_decode(text[i:])
            except ValueError:
                continue
            if isinstance(d, dict) and "verdict" in d:
                found = d
    if found:
        v = str(found.get("verdict", "")).strip().lower().replace(" ", "_")
        v = VERDICTS.get(v, v)          # the model's value, once normalised, is the stored one
        if v in ("ok", "needs_changes", "blocking"):
            points = [str(p)[:400] for p in (found.get("points") or [])][:12]
            # The verdict follows the labels of the points: "to review again" only when something must be fixed before merging.
            must = any(MUST.match(p) for p in points)
            if v == "needs_changes" and not must and any(LATER.match(p) for p in points):
                v = "ok"
            elif v == "ok" and must:
                v = "needs_changes"
            return (v, str(found.get("summary") or "")[:2000], points,
                    str(found.get("evidence") or "").strip()[:1000] if v == "blocking" else "")
    return "undetermined", "Unreadable reviewer answer: to be reviewed by a human.", [], ""


VERDICTS = {"needs_changes": "needs_changes", "blocking": "blocking"}
MUST = re.compile(r"^\s*\**\s*must\s+fix\s*\**\s*:", re.I)
LATER = re.compile(r"^\s*\**\s*later\s*\**\s*:", re.I)
TOOLS = {"direct": "You have no tool: everything you can review is in the diff below. If it is truncated, say so in "
                    "your summary; do not guess the rest.",
         "agent": "The diff is provided below: read a file only if the diff is not enough, and answer quickly."}


REVIEW_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def mode(model):
    if config.REVIEW_MODE != "direct":
        return "agent"
    if model.get("provider") == "anthropic":
        return "direct" if (model.get("key") or config.ANTHROPIC_API_KEY) else "agent"
    return "direct" if model.get("base_url") else "agent"


def prompts(ctx, role, diff, how):
    label, role_name, mission = ROLES[role]
    from .messages import language
    system = TEMPLATE.format(role_label=role_name, mission=mission, tools=TOOLS[how], language=language((getattr(ctx, "repo", None) or {}).get("lang")))
    extra = ""
    report = getattr(ctx, "security_report", "") or ""
    if role == "security" and report:
        # Results of the automatic check (Betterleaks, package registries): leads to verify, not verdicts.
        extra = (f"<automatic_check>\n{data(report[:4000])}\n</automatic_check>\n\nThe block above comes from automatic "
                 "tools. These are leads: verify each one in the diff. Discard false positives (a dummy value in "
                 "a test, for example) by saying so in a point, and take up the real problems in your points.\n\n")
    if role == "code":
        extra = ("Start your points with one point per \"Done when\" criterion (or, if it is not specified, per requirement of the "
                 "request), in this exact form, with these English words whatever the language of the rest: \"Criterion met: …\", "
                 "\"Criterion not met: …\" or \"Criterion unclear: …\", followed by a short explanation drawn from the diff.\n\n")
    changed = getattr(ctx, "tests_changed", None) or []
    if changed:
        extra += ("These test files ALREADY existed before this change, and it modifies or deletes them: "
                  + ", ".join(data(f) for f in changed[:20])
                  + ". Weakening, rewriting or removing a test is the easiest way to make the checks pass: say in a point whether each "
                  "change is justified by the request, or whether it hides a regression.\n\n")
    prompt = (f"<request>\nTitle: {data(ctx.title)}\n\n{data(ctx.job['request'])}\n\nDone when: {data(ctx.job.get('done_when')) or '(not specified)'}\n</request>\n\n"
              f"<diff>\n{data(diff)}\n</diff>\n\n{extra}Review for your role ({role_name}) and answer only with the requested JSON block.")
    return label, system, prompt


EFFORT = {1: "trivial", 2: "light", 3: "medium", 4: "substantial", 5: "heavy"}


def effort(lines, files, protected=False):
    """Review effort, from 1 to 5, calculated based on the size of the change (without model); +1 if a protected path is touched."""
    n = 1 if lines <= 30 and files <= 2 else 2 if lines <= 120 and files <= 5 else 3 if lines <= 300 and files <= 10 else 4 if lines <= 800 else 5
    return min(5, n + (1 if protected else 0))


# The reviewer marks each criterion with fixed English words, whatever the language of the rest of its text.
CRITERION = re.compile(r"^\s*criterion\s+(met|not\s+met|unclear)\s*:\s*(.+)$", re.I | re.S)


def criteria(points):
    """Code reviewer points evaluating a request criterion: [(state, text)], state = ok | no | unclear."""
    out = []
    for p in points or []:
        m = CRITERION.match(str(p))
        if m:
            k = m.group(1).lower()
            out.append(("no" if k.startswith("not") else "unclear" if k == "unclear" else "ok", m.group(2).strip()))
    return out


CAUSE_CUT = "answer cut by the model's token limit before the verdict"
CAUSE_NOJSON = "no JSON verdict in the model's answer"


def ask(model, system, prompt, timeout, transport=None, think=True):
    """A request to the model; returns (text, cost in $, cut by the token limit?). think=False cuts the thinking
    of a local "thinking" model (llama.cpp, vLLM: chat_template_kwargs.enable_thinking)."""
    if model.get("provider") == "anthropic":
        with httpx.Client(base_url=config.ANTHROPIC_API_URL, timeout=timeout, transport=transport) as h:
            r = h.post("/v1/messages", headers={"x-api-key": model.get("key") or config.ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"},
                       json={"model": model["id"], "max_tokens": config.REVIEW_MAX_TOKENS, "system": system,
                             "messages": [{"role": "user", "content": prompt}]})
            r.raise_for_status()
            m = r.json()
        u = m.get("usage") or {}
        pin, pout, _ = config.price(model["id"], model)
        cost = (u.get("input_tokens", 0) * pin + u.get("output_tokens", 0) * pout) / 1e6
        cut = m.get("stop_reason") == "max_tokens"
        if cut:
            log.warning("review cut by the token limit (REVIEW_MAX_TOKENS=%s)", config.REVIEW_MAX_TOKENS)
        return "".join(b.get("text", "") for b in m.get("content") or [] if b.get("type") == "text"), cost, cut
    base = model["base_url"].rstrip("/")
    base = base if base.endswith("/v1") else base + "/v1"
    with httpx.Client(timeout=timeout, transport=transport) as h:
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        if model.get("type") == "openai":   # Official API: reasoning models, no temperature, other name for limit
            body = {"model": model["id"], "max_completion_tokens": config.REVIEW_MAX_TOKENS, "messages": msgs,
                    "reasoning_effort": "medium" if think else "low"}
        else:
            body = {"model": model["id"], "temperature": 0, "max_tokens": config.REVIEW_MAX_TOKENS, "messages": msgs}
            if not think:
                body["chat_template_kwargs"] = {"enable_thinking": False}
        r = h.post(base + "/chat/completions", headers={"Authorization": f"Bearer {model.get('key') or 'local'}"}, json=body)
        r.raise_for_status()
        m = r.json()
    ch = (m.get("choices") or [{}])[0]
    u = m.get("usage") or {}
    cost = 0.0
    if model.get("type") == "openai":
        pin, pout, _ = config.price(model["id"], model)
        cost = (u.get("prompt_tokens", 0) * pin + u.get("completion_tokens", 0) * pout) / 1e6
    cut = ch.get("finish_reason") == "length"   # the model's thinking exhausted the limit before the verdict
    if cut:
        log.warning("review cut by the token limit (REVIEW_MAX_TOKENS=%s)", config.REVIEW_MAX_TOKENS)
    return str((ch.get("message") or {}).get("content") or ""), cost, cut


def reviewer(ctx, role):
    """Model of a reviewer: the profile's model for this role, otherwise the reviewers' model, otherwise the agent's."""
    return (getattr(ctx, "review_models", None) or {}).get(role) or getattr(ctx, "review_model", None) or ctx.model


def run(ctx, role, diff, transport=None):
    rm = reviewer(ctx, role)
    how = mode(rm)
    label, system, prompt = prompts(ctx, role, diff, how)
    if how == "direct":
        note = ""
        try:
            text, cost, cut = ask(rm, system, prompt, config.REVIEW_TIMEOUT, transport)
            if cut and rm.get("type", "compatible") == "compatible" and rm.get("provider") != "anthropic":
                # The model went in circles in its thinking: same review, cut thinking, rather than no verdict.
                log.warning("%s: review %s: thinking too long, asking again without thinking", ctx.ref, role)
                text, cost2, cut = ask(rm, system, prompt, config.REVIEW_TIMEOUT, transport, think=False)
                cost += cost2
                note = "Verdict given without thinking: the model's thinking exceeded the token limit."
        except httpx.TimeoutException:
            return unreadable(role, f"the model did not answer in {config.REVIEW_TIMEOUT} s")
        except (httpx.HTTPError, ValueError, KeyError) as e:
            log.warning("%s: review %s impossible (%s)", ctx.ref, role, type(e).__name__)
            return unreadable(role, f"model unreachable or in error ({type(e).__name__})")
        verdict, summary, points, proof = parse(text)
        if verdict == "undetermined":
            return unreadable(role, CAUSE_CUT if cut else CAUSE_NOJSON, cost)
        return {"role": role, "label": label, "verdict": verdict, "summary": summary, "points": points + ([note] if note else []),
                "proof": proof, "cost": cost}
    menv, local = model_env(rm, ctx.ex.gateway_url(), ctx.fab_token)
    # The reviewer owes nothing to the job it judges: program launched by its absolute path, system PATH only
    # (not the job's HOME folders), fresh personal directory and reading an export of the verified commit, both
    # prepared by the runner after the job processes have stopped (Runner.reviews).
    where, home = getattr(ctx, "review_dir", None) or ctx.repo_dir, getattr(ctx, "review_home", None) or ctx.home
    env = base_env(home, {"FAB_WORKSPACE": where, "DISABLE_AUTOUPDATER": "1", "DISABLE_TELEMETRY": "1", **menv})
    env["PATH"] = REVIEW_PATH
    cmd = [config.CLAUDE_BIN, "-p", prompt, "--output-format", "json", "--max-turns", "8",
                        "--allowedTools", "Read Glob Grep", "--disallowedTools", "Edit Write MultiEdit Bash WebFetch WebSearch Task",
                        "--append-system-prompt", system, "--model", rm["id"]]
    try:
        p = ctx.ex.run(ctx, cmd, env, where, config.REVIEW_TIMEOUT)
        out = json.loads(p.stdout or "{}")
    except (RuntimeError, ValueError) as e:
        log.warning("%s: review %s unreadable (%s)", ctx.ref, role, type(e).__name__)
        return unreadable(role, M("reviewer interrupted ({why})", why=type(e).__name__))
    verdict, summary, points, proof = parse(out.get("result", ""))
    cost = 0.0 if local else float(out.get("total_cost_usd") or 0)
    if verdict == "undetermined":
        return unreadable(role, M("timed out") if p.returncode == 124 else CAUSE_NOJSON, cost)
    return {"role": role, "label": label, "verdict": verdict, "summary": summary, "points": points, "proof": proof, "cost": cost}


def unreadable(role, cause, cost=0.0):
    """Review without actionable verdict: never "ok"; the cause is stated, so the human knows what to do."""
    label = ROLES[role][0]
    return {"role": role, "label": label, "verdict": "undetermined", "cause": cause, "points": [], "proof": "", "cost": cost,
            "summary": f"Unreadable review: {cause}. This is not a negative opinion: no verdict was given."}


def confirm(r, replay):
    """A "blocking" is only retained if its evidence, replayed by the runner in the job workspace, fails.
    replay(cmd) -> (exit code, output). Otherwise it becomes "to be reviewed" (neither false blocking, nor "ok")."""
    if r["verdict"] != "blocking":
        return r
    if not r.get("proof"):
        return {**r, "verdict": "needs_changes", "summary": "Blocking point not demonstrated (no evidence provided): " + r["summary"]}
    code, out = replay(r["proof"])
    shown = f"Evidence replayed by the runner: `{r['proof'][:200]}` → code {code}"
    if code == 0:
        return {**r, "verdict": "needs_changes", "summary": "Blocking point not demonstrated (the evidence succeeds): " + r["summary"],
                "points": r["points"] + [shown]}
    return {**r, "points": r["points"] + [shown + (f" : {out.strip()[-300:]}" if out.strip() else "")]}
