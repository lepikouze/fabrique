"""Browser check of the live updates (needs Playwright; not run by the CI). For each view: "change" events received,
fragment reloads and DOM updates in the live areas during N seconds, while a job is moving.
Usage: live_check.py <base url> <seconds> <label> [par|comma-separated view names] [job ref]
The instance must have a local account `mesure` (see the constants below) or be at its setup page with the code below."""
import asyncio, json, sys
from playwright.async_api import async_playwright
BASE, SECS, LABEL = sys.argv[1], int(sys.argv[2]), sys.argv[3]
INIT = """
window.__m = {changes: 0, pings: 0, opens: 0, errors: 0, fetches: 0, live: 0, panel: 0, lastChange: 0, delays: []};
(function () {
  var ES = window.EventSource;
  if (ES) window.EventSource = function (u, o) {
    var e = new ES(u, o);
    e.addEventListener('open', function () { __m.opens++; });
    e.addEventListener('error', function () { __m.errors++; });
    e.addEventListener('change', function () { __m.changes++; __m.lastChange = performance.now(); });
    e.addEventListener('ping', function () { __m.pings++; });
    return e;
  };
  var f = window.fetch; window.fetch = function () { __m.fetches++; return f.apply(this, arguments); };
  document.addEventListener('DOMContentLoaded', function () {
    new MutationObserver(function (ms) {
      var hit = {live: false, panel: false};
      ms.forEach(function (m) {
        var t = m.target.nodeType === 1 ? m.target : m.target.parentElement;
        if (!t) return;
        if (t.closest('#panel')) hit.panel = true; else if (t.closest('[data-live]')) hit.live = true;
      });
      if (hit.live) __m.live++;
      if (hit.panel) __m.panel++;
      if ((hit.live || hit.panel) && __m.lastChange) { __m.delays.push(Math.round(performance.now() - __m.lastChange)); __m.lastChange = 0; }
    }).observe(document.documentElement, {childList: true, subtree: true, characterData: true});
  });
})();
"""
async def setup(pg):
    await pg.goto(BASE + "/")
    if "/auth/setup" in pg.url:
        for k, v in (("code", "AAAA-BBBB-CCCC-DDDD"), ("login", "check"), ("name", "Check"), ("password", "Tableau-Direct-2026!"), ("password2", "Tableau-Direct-2026!")):
            if await pg.query_selector(f"input[name={k}]"): await pg.fill(f"input[name={k}]", v)
        await pg.click("form button"); await pg.wait_for_load_state(); await pg.wait_for_timeout(800)
    elif "/auth/login" in pg.url:
        for k, v in (("login", "check"), ("username", "check"), ("password", "Tableau-Direct-2026!")):
            if await pg.query_selector(f"input[name={k}]"): await pg.fill(f"input[name={k}]", v)
        await pg.click("form button"); await pg.wait_for_load_state(); await pg.wait_for_timeout(800)
async def view(ctx, name, path, click=None):
    pg = await ctx.new_page(); errs = []
    pg.on("console", lambda m: errs.append(m.text[:120]) if m.type == "error" else None)
    await pg.goto(BASE + path); await pg.wait_for_load_state()
    if click:
        el = await pg.query_selector(click)
        if el: await el.click(); await pg.wait_for_timeout(1500)
    await pg.evaluate("Object.assign(__m, {changes: 0, pings: 0, fetches: 0, live: 0, panel: 0, delays: []})")
    await pg.wait_for_timeout(SECS * 1000)
    m = await pg.evaluate("__m"); d = sorted(m["delays"])
    med = d[len(d) // 2] if d else None
    print(json.dumps({"version": LABEL, "view": name, "url": pg.url.replace(BASE, ""), "connections": m["opens"], "stream_errors": m["errors"], "changes": m["changes"], "pings": m["pings"],
                      "reloads": m["fetches"], "area_updates": m["live"], "panel_updates": m["panel"], "median_delay_ms": med, "console": errs[:3]}, ensure_ascii=False))
    await pg.close()
async def main():
    mode = sys.argv[4] if len(sys.argv) > 4 else "seq"; ref = sys.argv[5] if len(sys.argv) > 5 else ""
    async with async_playwright() as p:
        b = await p.chromium.launch(); ctx = await b.new_context(viewport={"width": 1440, "height": 900}, locale="fr-FR")
        await ctx.add_init_script(INIT)
        pg = await ctx.new_page(); await setup(pg); await pg.close()
        sel = '[data-ref="%s"]' % ref if ref else "[data-ref]"
        views = (("jobs board", "/jobs", None), ("jobs list", "/jobs?view=list", None), ("panel", "/jobs", sel), ("full view", "/jobs/%s" % ref, None), ("inbox", "/inbox", None))
        if mode == "par":
            await asyncio.gather(*[view(ctx, n + " (5 tabs)", pa, c) for n, pa, c in views])
        else:
            for n, pa, c in views:
                if n in mode.split(","): await view(ctx, n + " (alone)", pa, c)
        await b.close()
asyncio.run(main())
