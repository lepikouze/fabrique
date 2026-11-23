// Fabrique: dynamic side panel, keyboard shortcuts, real-time (SSE), toasts.
// Everything also works without JavaScript (classic links and forms); this script only speeds things up.
// Texts from this script in the page language: the server provides them in <script id="i18n" type="application/json">.
var I18N = {};
try { I18N = JSON.parse((document.getElementById("i18n") || {}).textContent || "{}"); } catch (e) { I18N = {}; }
function T(s) { return I18N[s] || s; }

(function () {
  "use strict";
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  var store = {
    get: function (k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set: function (k, v) { try { localStorage.setItem(k, v); } catch (e) {} }
  };
  var page = document.body.dataset.page;

  // ---- Theme and sidebar ----
  function markTheme() {
    var cur = document.documentElement.getAttribute("data-theme-choice") || "dark";
    $$("[data-theme-set]").forEach(function (b) { b.setAttribute("aria-checked", b.dataset.themeSet === cur ? "true" : "false"); });
  }
  $$("[data-theme-set]").forEach(function (b) {
    b.addEventListener("click", function () { store.set("fabrique-theme", b.dataset.themeSet); window.fabriqueTheme(b.dataset.themeSet); markTheme(); });
  });
  markTheme();
  // User menu: closes when clicking elsewhere or on Esc
  document.addEventListener("click", function (e) { $$("details.usermenu[open]").forEach(function (m) { if (!m.contains(e.target)) m.open = false; }); });
  function toggleSide() {
    // Small screen: the bar is always collapsed; the button opens it over the page, with its labels.
    if (window.matchMedia("(max-width: 900px)").matches) { document.documentElement.classList.toggle("side-open"); return; }
    var on = document.documentElement.classList.toggle("side-min");
    store.set("fabrique-side", on ? "min" : "full");
  }
  document.addEventListener("click", function (e) {      // a click outside the open bar closes it
    var h = document.documentElement;
    if (h.classList.contains("side-open") && !e.target.closest(".side") && !e.target.closest("[data-sidebar-toggle]")) h.classList.remove("side-open");
  });
  $$("[data-sidebar-toggle]").forEach(function (b) { b.addEventListener("click", toggleSide); });

  // Confirmation before a destructive action
  // Confirmation (delegation): live refresh can replace the form while the question is
  // opened; a detached form is no longer sent by the browser. We therefore send a copy, attached to the page.
  document.addEventListener("submit", function (e) {
    var f = e.target;
    if (!f.matches || !f.matches("form[data-confirm]") || f.dataset.confirmed) return;
    e.preventDefault();
    if (!confirm(f.dataset.confirm)) return;
    var g = f.cloneNode(true);
    g.dataset.confirmed = "1"; g.hidden = true;
    document.body.appendChild(g);
    g.submit();
  });

  // Selectors that send themselves
  // A list that submits its form when its value changes. With the keyboard, arrows change the value of a closed list at each
  // press: we then wait for Enter, or for the focus to leave, so that moving through the choices does not reload the page.
  $$("[data-autosubmit]").forEach(function (s) {
    var byKey = false, start = s.value;
    function go() { if (s.value !== start) { start = s.value; s.form.submit(); } }
    s.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { byKey = false; go(); } else if (e.key !== "Tab") { byKey = true; }
    });
    s.addEventListener("pointerdown", function () { byKey = false; });
    s.addEventListener("change", function () { if (!byKey) { go(); } });
    s.addEventListener("blur", function () { if (byKey) { byKey = false; go(); } });
  });

  // ---- Dialog boxes ----
  $$("[data-open-dialog]").forEach(function (b) {
    b.addEventListener("click", function () {
      $$("details.usermenu[open]").forEach(function (m) { m.open = false; });   // the menu closes behind the window
      var d = document.getElementById(b.dataset.openDialog); if (d) d.showModal();
    });
  });
  $$("[data-close-dialog]").forEach(function (b) { b.addEventListener("click", function () { b.closest("dialog").close(); }); });
  // A click outside a modal window (on the background) closes it.
  $$("dialog.dlg").forEach(function (d) {
    d.addEventListener("click", function (e) {
      if (e.target !== d) return;
      var r = d.getBoundingClientRect();
      if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) d.close();
    });
  });

  // ---- Toasts ----
  function toast(text, kind) {
    var box = $("#toasts"), el = document.createElement("div");
    el.className = "toast " + (kind || "ok"); el.textContent = text;
    box.appendChild(el); fade(el);
  }
  function fade(el) { setTimeout(function () { el.classList.add("bye"); setTimeout(function () { el.remove(); }, 400); }, 4200); }
  $$("#toasts .toast").forEach(fade);

  // ---- Ctrl+↵ (or Cmd+↵) sends the response form ----
  document.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      var f = e.target.closest && e.target.closest("form[data-cmd-enter]");
      if (f) { e.preventDefault(); f.requestSubmit(); }
    }
  });

  // ---- Side panel (jobs, inbox) ----
  var panel = $("#panel");
  var kind = panel && panel.dataset.kind;
  var openRef = (function () {
    var q = new URLSearchParams(location.search);
    return q.get("job") || q.get("item") || (panel && panel.dataset.open) || null;
  })();

  // Inbox: the first item is open by default, except on small screens where the pane would cover the list.
  if (panel && panel.classList.contains("auto")) {
    if (window.matchMedia("(max-width: 900px)").matches) { openRef = null; panel.hidden = true; panel.innerHTML = ""; }
    panel.classList.remove("auto");
  }
  // OIDC Connection: attempt discovery without reloading the page (otherwise unsaved input would be lost).
  var oidcTest = $("[data-oidc-test]");
  if (oidcTest) oidcTest.addEventListener("click", function () {
    var form = oidcTest.closest("form"), out = $("[data-oidc-test-out]", form);
    out.hidden = false; out.className = "git-test muted"; out.textContent = T("Testing…"); oidcTest.disabled = true;
    fetch("/settings-test/oidc", { method: "POST", body: new FormData(form), credentials: "same-origin", headers: { "Accept": "application/json" } })
      .then(function (r) { return r.json(); })
      .then(function (d) { out.className = "git-test " + (d.ok ? "ok" : "err"); out.textContent = d.msg; })
      .catch(function () { out.className = "git-test err"; out.textContent = T("The test did not complete: reload the page and try again."); })
      .then(function () { oidcTest.disabled = false; });
  });
  // Git provider: attempt token validation before saving (nothing is written on the server side).
  var gitTest = $("[data-git-test]");
  if (gitTest) gitTest.addEventListener("click", function () {
    var form = gitTest.closest("form"), out = $("[data-git-test-out]", form);
    out.hidden = false; out.className = "git-test muted"; out.textContent = T("Testing…"); gitTest.disabled = true;
    fetch("/settings-test/git", { method: "POST", body: new FormData(form), credentials: "same-origin", headers: { "Accept": "application/json" } })
      .then(function (r) { return r.json(); })
      .then(function (d) { out.className = "git-test " + (d.ok ? "ok" : "err"); out.textContent = d.msg; })
      .catch(function () { out.className = "git-test err"; out.textContent = T("The test did not complete: reload the page and try again."); })
      .then(function () { gitTest.disabled = false; });
  });
  // New request: labels and examples according to the chosen type (change or study).
  var kinded = $("[data-kinded]");
  if (kinded) {
    var setKind = function () {
      var study = !!kinded.querySelector('input[name="study"][value="1"]:checked');
      kinded.classList.toggle("is-study", study);
      $$("[data-ph-change]", kinded).forEach(function (el) { el.placeholder = study ? el.dataset.phStudy : el.dataset.phChange; });
    };
    $$('input[name="study"]', kinded).forEach(function (r) { r.addEventListener("change", setKind); });
    setKind();
  }
  // New request: plain signals while typing, without any model. A very short need, and no "Done when" criterion.
  var needEl = $("#n-need"), doneEl = $("#n-done"), hintShort = $("[data-hint-short]"), hintDone = $("[data-hint-done]");
  if (kinded && needEl && hintShort) {
    var hints = function () {
      var study = kinded.classList.contains("is-study");
      var text = needEl.value.replace(/\s+/g, " ").trim();
      var words = text ? text.split(" ").length : 0;
      hintShort.hidden = !text || !(text.length < +hintShort.dataset.shortChars && words < +hintShort.dataset.shortWords);
      if (hintDone) hintDone.classList.toggle("hint-warn", !study && !!text && !doneEl.value.trim());
    };
    [needEl, doneEl].forEach(function (el) { if (el) el.addEventListener("input", hints); });
    $$('input[name="study"]', kinded).forEach(function (r) { r.addEventListener("change", hints); });
    hints();
  }
  // New request: the summary of what will apply follows the chosen repo.
  var repoSel = $("#n-repo");
  if (repoSel) {
    var showRepo = function () { $$("[data-repo-info]").forEach(function (el) { el.hidden = el.dataset.repoInfo !== repoSel.value; }); };
    repoSel.addEventListener("change", showRepo); showRepo();
  }

  function panelUrl(ref) { return kind === "inbox" ? "/inbox/" + encodeURIComponent(ref) + "/panel" : "/jobs/" + encodeURIComponent(ref) + "/panel"; }
  function pageUrl(ref) {
    var q = new URLSearchParams(location.search);
    q.delete("job"); q.delete("item");
    if (ref) q.set(kind === "inbox" ? "item" : "job", ref);
    var s = q.toString();
    return location.pathname + (s ? "?" + s : "");
  }
  function markSelected(ref) {
    $$("[data-row]").forEach(function (r) { r.classList.toggle("sel", !!ref && r.dataset.ref === ref); });
    var wrap = $(".jobs-wrap"); if (wrap) wrap.classList.toggle("with-panel", !!ref);
  }
  // State to preserve when a zone is replaced: scroll positions and <details> sections open or closed.
  function keyOf(d, i) { var s = d.querySelector("summary"); return (d.dataset.key || (s ? s.textContent.trim().slice(0, 80) : "")) + "#" + i; }
  function saveState(root) {
    var st = { scroll: [], open: {} };
    $$(".board, .col-b, .fcol, .p-body, .list, .events", root).forEach(function (el, i) { st.scroll.push([i, el.scrollLeft, el.scrollTop]); });
    if (root.scrollTop || root.scrollLeft) st.self = [root.scrollLeft, root.scrollTop];
    $$("details", root).forEach(function (d, i) { st.open[keyOf(d, i)] = d.open; });
    st.typed = [];   // typed text not yet sent: an ongoing response must never disappear
    $$("textarea[name], input[type=text][name]", root).forEach(function (t) {
      if (t.value !== t.defaultValue) st.typed.push([fieldKey(t), t.value]);
    });
    return st;
  }
  function fieldKey(t) { var f = t.form; return (f ? f.getAttribute("action") || "" : "") + "|" + t.name; }
  function restoreState(root, st) {
    // Sections first, then scrolling: as long as a section opened by the reviewer is still collapsed, the zone
    // is shorter and the browser clamps the requested position (we ended up at the top or bottom of the job).
    $$("details", root).forEach(function (d, i) { var k = keyOf(d, i); if (k in st.open) d.open = st.open[k]; });
    var els = $$(".board, .col-b, .fcol, .p-body, .list, .events", root);
    st.scroll.forEach(function (x) { var el = els[x[0]]; if (el) { el.scrollLeft = x[1]; el.scrollTop = x[2]; } });
    if (st.self) { root.scrollLeft = st.self[0]; root.scrollTop = st.self[1]; }
    (st.typed || []).forEach(function (x) {
      var t = $$("textarea[name], input[type=text][name]", root).filter(function (e) { return fieldKey(e) === x[0]; })[0];
      if (t && t.value === t.defaultValue) t.value = x[1];
    });
  }

  // ---- Job diff: loaded when opening "Read diff", kept in memory to survive refreshes ----
  var diffCache = {};
  function fillDiffs(root) {   // after replacing a zone: restores the already loaded diff before restoring the <details>
    $$("details[data-diff]", root || document).forEach(function (d) {
      var body = d.querySelector("[data-diff-body]"), html = diffCache[d.dataset.diff];
      if (body && html && !body.dataset.loaded) { body.innerHTML = html; body.dataset.loaded = "1"; }
    });
  }
  function loadDiff(d) {
    var body = d.querySelector("[data-diff-body]"), url = d.dataset.diff;
    if (!body || body.dataset.loaded || body.dataset.loading) return Promise.resolve();
    if (diffCache[url]) { fillDiffs(d.parentNode); return Promise.resolve(); }
    body.dataset.loading = "1";
    return fetch(url, { headers: { "Accept": "text/html" }, credentials: "same-origin" }).then(function (r) {
      if (r.status === 401) { location.reload(); return; }
      return r.text().then(function (html) { if (r.ok) diffCache[url] = html; body.innerHTML = html; body.dataset.loaded = "1"; });
    }).catch(function () { body.textContent = T("Diff unavailable for now."); }).then(function () { delete body.dataset.loading; });
  }
  document.addEventListener("toggle", function (e) {
    var d = e.target;
    if (d && d.matches && d.matches("details[data-diff]") && d.open) loadDiff(d);
  }, true);
  document.addEventListener("click", function (e) {   // a file from the list: opens the diff, expanded on this file
    var a = e.target.closest && e.target.closest("[data-diff-file]");
    if (!a || e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
    var sec = a.closest(".sec"), d = sec && sec.querySelector("details[data-diff]");
    if (!d) return;
    e.preventDefault();
    d.open = true;
    loadDiff(d).then(function () {
      var f = d.querySelectorAll("details.dfile")[+a.dataset.diffFile];
      if (f) { f.open = true; f.scrollIntoView({ block: "start", behavior: "smooth" }); }
    });
  });

  function loadPanel(ref, push) {
    if (!panel || (kind !== "job" && kind !== "inbox")) return false;
    return fetch(panelUrl(ref), { headers: { "Accept": "text/html" }, credentials: "same-origin" }).then(function (r) {
      if (r.status === 401) { location.reload(); return; }
      if (!r.ok) { if (push) location.href = pageUrl(ref); return; }
      return r.text().then(function (html) {
        var same = openRef === ref && !push;
        var typing = panel.contains(document.activeElement) && /TEXTAREA|INPUT/.test(document.activeElement.tagName) && document.activeElement.value;
        if (!push && typing) return;   // never overwrite a response currently being typed
        if (same && panel._served === html) return;   // nothing has changed in this job: leave the page alone
        var st = same ? saveState(panel) : null;
        panel.innerHTML = html; panel._served = html; panel.hidden = false; wire(panel);
        fillDiffs(panel);
        if (st) restoreState(panel, st);
        openRef = ref; markSelected(ref);
        if (push) history.pushState({ ref: ref }, "", pageUrl(ref));
      });
    });
  }
  function closePanel(push) {
    if (!panel) return;
    panel.hidden = true; panel.innerHTML = ""; panel._served = null; openRef = null; markSelected(null);
    if (push) history.pushState({ ref: null }, "", pageUrl(null));
  }
  function wire(root) {
    $$("[data-close]", root).forEach(function (a) { a.addEventListener("click", function (e) { e.preventDefault(); closePanel(true); }); });
    $$("[data-cmd-enter] textarea", root).forEach(function (t) { if (t.hasAttribute("autofocus")) t.focus(); });
  }
  if (panel) wire(panel);
  // Machine translation of a job's texts: one click shows the originals, another the translations.
  document.addEventListener("click", function (e) {
    var b = e.target.closest && e.target.closest("[data-mt-toggle]");
    if (!b) return;
    var root = b.closest(".p-body, .full, main") || document, original = b.dataset.shown !== "original";
    $$("[data-mt]", root).forEach(function (el) { el.hidden = original; });
    $$("[data-mo]", root).forEach(function (el) { el.hidden = !original; });
    b.dataset.shown = original ? "original" : "translated";
    b.textContent = original ? b.dataset.translated : b.dataset.original;
  });
  document.addEventListener("click", function (e) {
    var a = e.target.closest && e.target.closest("a[data-row][data-ref]");
    if (!a || !panel || e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
    e.preventDefault(); loadPanel(a.dataset.ref, true);
  });
  window.addEventListener("popstate", function () {
    var q = new URLSearchParams(location.search), ref = q.get("job") || q.get("item");
    if (ref) loadPanel(ref, false); else closePanel(false);
  });

  // ---- Shortcuts: J/K navigate, ↵ open, Esc close, [ sidebar ----
  var cursor = -1;
  function rows() { return $$("[data-row]").filter(function (r) { return r.offsetParent !== null; }); }
  document.addEventListener("keydown", function (e) {
    var t = e.target;
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (t && (/INPUT|TEXTAREA|SELECT/.test(t.tagName) || t.isContentEditable)) {
      if (e.key === "Escape") t.blur();
      return;
    }
    if ($("dialog[open]")) return;
    var list = rows();
    if (e.key === "j" || e.key === "k") {
      if (!list.length) return;
      var cur = list.findIndex(function (r) { return r === document.activeElement; });
      if (cur < 0) cur = list.findIndex(function (r) { return r.classList.contains("sel"); });
      cursor = Math.max(0, Math.min(list.length - 1, (cur < 0 ? -1 : cur) + (e.key === "k" ? 1 : -1)));
      list[cursor].focus(); list[cursor].scrollIntoView({ block: "nearest" });
      e.preventDefault();
    } else if (e.key === "Enter" && document.activeElement && document.activeElement.matches("[data-row]")) {
      e.preventDefault(); document.activeElement.click();
    } else if (e.key === "Escape") {
      var um = $("details.usermenu[open]");
      if (um) { um.open = false; return; }
      var shrink = $("[data-shrink]");
      if (shrink) { location.href = shrink.getAttribute("href"); return; }
      if (panel && !panel.hidden) { e.preventDefault(); closePanel(true); }
    } else if ((e.key === "b" || e.key === "B") && !e.ctrlKey && !e.metaKey && !e.altKey) {
      toggleSide();
    }
  });

  // ---- Real-time: on every database change, we refresh [data-live] areas and the open dashboard ----
  var live = $("#live") || ($("[data-live]") ? document.createElement("span") : null);   // full view: no indicator, but live
  var pending = null, busy = false;
  function refresh() {
    if (busy) { pending = pending || setTimeout(refresh, 600); return; }
    busy = true; pending = null;
    fetch(location.pathname + location.search, { headers: { "Accept": "text/html" }, credentials: "same-origin" }).then(function (r) {
      if (r.status === 401) { location.reload(); return; }
      if (!r.ok) return;
      return r.text().then(function (html) {
        var doc = new DOMParser().parseFromString(html, "text/html");
        $$("[data-live]").forEach(function (el) {
          var fresh = doc.querySelector('[data-live="' + el.dataset.live + '"]');
          if (!fresh) return;
          if (el.contains(document.activeElement) && /TEXTAREA|INPUT|SELECT/.test(document.activeElement.tagName)) return;
          if ((el._served || el.innerHTML) === fresh.innerHTML) return;   // nothing has changed: do not touch anything
          var st = saveState(el), main = $("#main"), mainScroll = main ? [main.scrollLeft, main.scrollTop] : null;
          var focusRef = document.activeElement && document.activeElement.dataset ? document.activeElement.dataset.ref : null;
          el.innerHTML = fresh.innerHTML; el._served = fresh.innerHTML;   // (a diff loaded on request is added afterwards)
          fillDiffs(el);
          if (fresh.className !== el.className && el.classList.contains("jobs-wrap")) el.className = fresh.className;
          restoreState(el, st);
          if (main && mainScroll) { main.scrollLeft = mainScroll[0]; main.scrollTop = mainScroll[1]; }
          if (focusRef) { var f = el.querySelector('[data-row][data-ref="' + focusRef + '"]'); if (f) f.focus({ preventScroll: true }); }
        });
        var badge = doc.querySelector('.nav[href="/inbox"]'), cur = $('.nav[href="/inbox"]');
        if (badge && cur) cur.innerHTML = badge.innerHTML;
        markSelected(openRef);
      });
    }).then(function () {
      if (openRef && panel && !panel.hidden) return loadPanel(openRef, false);
    }).catch(function () {}).then(function () { busy = false; });
  }
  // Real time. Over HTTP/2 or HTTP/3 (the usual case behind a TLS reverse proxy) every tab opens its own connection:
  // requests are multiplexed, there is no limit to work around. Behind an intermediary in HTTP/1.1 a browser only opens
  // 6 connections per site, and one per tab was enough to block the next page: there, a "leader" tab (Web Locks) holds
  // the only connection and relays changes to the others (BroadcastChannel).
  // A follower never trusts the leader blindly: it asks "hello" and expects an answer within 3 seconds, again and again.
  // Without an answer (a tab left open with a lost session, a page from an older version, a frozen tab), or when the
  // leader says its own connection is down, the follower opens its own connection at once instead of staying frozen
  // behind a green light.
  if (window.EventSource && live) {
    var es = null, bc = null, leader = false, retry = null, delay = 2000, heard = 0, asked = 0, own = false;
    var diag = { role: "", last: 0, updates: 0, errors: 0 };
    var tell = function () {                             // what the indicator says when hovered: the state, for a diagnosis
      var role = diag.role === "leader" ? T("this tab holds the connection") : diag.role === "follower" ? T("through another tab") : T("own connection");
      var state = live.dataset.state === "on" ? T("connected") : live.dataset.state === "off" ? T("connection lost, retrying") : T("connecting");
      var last = diag.last ? T("last change {n} s ago").replace("{n}", Math.round((Date.now() - diag.last) / 1000)) : T("no change received yet");
      live.title = T("Live updates") + " · " + state + " · " + role + " · " + last + " · " + T("{n} update(s) on this page").replace("{n}", diag.updates);
      live.dataset.role = diag.role;
    };
    live.addEventListener("mouseenter", function () { tell(); });
    var onChange = function () { diag.last = Date.now(); diag.updates++; clearTimeout(pending); pending = setTimeout(refresh, 250); };
    var setState = function (s) { live.dataset.state = s; if (leader && bc) bc.postMessage(s); tell(); };
    var open = function () {
      if (es) return;
      es = new EventSource("/events/stream");
      es.addEventListener("open", function () {
        var back = delay > 2000 || live.dataset.state === "off";
        delay = 2000; setState("on");
        if (back) onChange();                            // the page may have missed changes while the connection was down
      });
      es.addEventListener("change", function () { onChange(); if (leader && bc) bc.postMessage("change"); });
      es.addEventListener("ping", function () { if (leader && bc) bc.postMessage("ping"); });
      es.addEventListener("error", function () {
        diag.errors++; setState("off");
        // An EventSource retries by itself after a network cut, but gives up for good on an HTTP error (a dashboard
        // restarting behind its proxy answers 502): the connection is then reopened here, more and more slowly.
        if (es && es.readyState === 2) {
          es = null;
          clearTimeout(retry);
          retry = setTimeout(function () { refresh(); open(); }, delay);   // refresh(): a lost session reloads the page
          delay = Math.min(delay * 2, 30000);
        }
      });
    };
    var stop = function () { clearTimeout(retry); if (es) { es.close(); es = null; } };
    var alone = function () {                            // no usable leader: this tab takes care of itself
      if (leader || own) return;
      own = true; diag.role = "own"; open(); onChange();
    };
    var start = function () {
      var nav = window.performance && performance.getEntriesByType ? performance.getEntriesByType("navigation")[0] : null;
      var multiplexed = /^h[23]/.test((nav && nav.nextHopProtocol) || "");
      if (!multiplexed && window.BroadcastChannel && navigator.locks) {
        // "-2": pages of versions before 0.8.2 used other names; a tab of theirs left open no longer counts.
        bc = new BroadcastChannel("fabrique-live-2");
        bc.onmessage = function (m) {
          if (m.data === "hello") { if (leader) bc.postMessage(live.dataset.state); return; }   // "on", "off" or "connecting"
          if (leader || own) return;
          if (m.data === "off") { alone(); return; }     // the leader's own connection is down: do not wait for it
          heard = Date.now();
          if (m.data === "change") onChange();
          else if (m.data === "on") { live.dataset.state = "on"; tell(); }
        };
        diag.role = "follower"; live.dataset.state = "connecting"; tell();
        navigator.locks.request("fabrique-live-2", function () {
          leader = true; own = false; diag.role = "leader"; open(); tell();
          return new Promise(function (done) { window.addEventListener("pagehide", done, { once: true }); });
        });
        asked = Date.now(); bc.postMessage("hello");
        setInterval(function () {
          if (leader || own) return;
          if (heard < asked) { alone(); return; }        // the last "hello" got no answer in 3 seconds
          asked = Date.now(); bc.postMessage("hello");
        }, 3000);
      } else {
        diag.role = "own"; open();                       // HTTP/2 or 3, or a browser without these APIs: one connection per tab
      }
      tell();
    };
    // The connection is closed BEFORE the next page is requested, to make room for it.
    window.addEventListener("beforeunload", stop);
    window.addEventListener("pagehide", stop);
    window.addEventListener("pageshow", function (e) { if (e.persisted) location.reload(); });
    if (document.readyState === "complete") start(); else window.addEventListener("load", start, { once: true });
  }
})();


// Copy the event log (double square button): lines in chronological order, plain text.
document.addEventListener("click", async (e) => {
  const b = e.target.closest("[data-copy]");
  if (!b) return;
  const list = document.querySelector(b.dataset.copy);
  if (!list) return;
  const lines = [...list.querySelectorAll("li")].map((li) => li.innerText.replace(/\s+/g, " ").trim()).reverse();
  const ref = b.dataset.copy.replace(/^#ev-/, "");
  const text = (ref ? `Journal ${ref}\n` : "") + lines.join("\n") + "\n";
  let ok = false;
  try { await navigator.clipboard.writeText(text); ok = true; } catch (_) {
    const t = document.createElement("textarea"); t.value = text; document.body.appendChild(t); t.select();
    try { ok = document.execCommand("copy"); } catch (_) {} t.remove();
  }
  const old = b.innerHTML;
  b.innerHTML = '<svg class="ic" aria-hidden="true"><use href="#i-check"/></svg>';
  b.title = ok ? T("Copied") : T("Cannot copy");
  setTimeout(() => { b.innerHTML = old; b.title = T("Copy the log (chronological order)"); }, 1500);
});


// Agent ↔ model: an agent only proposes models that speak its API; provider type → pre-filled address.
function syncCompat(form) {
  const h = form.querySelector("[data-compat-harness]"), m = form.querySelector("[data-compat-model]");
  if (!h || !m) return;
  const harness = h.value;
  let firstOk = null;
  for (const o of m.options) {
    const ok = (o.dataset.harnesses || "claude-code").split(" ").includes(harness);
    o.disabled = !ok; o.hidden = !ok;
    if (ok && !firstOk) firstOk = o;
  }
  if (m.selectedOptions[0]?.disabled && firstOk) m.value = firstOk.value;
}
document.addEventListener("change", (e) => {
  if (e.target.matches("[data-compat-harness]")) syncCompat(e.target.form);
  if (e.target.matches("[data-provider-form] select[name=type]")) {
    const f = e.target.form, base = e.target.selectedOptions[0].dataset.base || "";
    const b = f.querySelector("[name=base_url]"); b.value = base; b.placeholder = base || "http://model-server:8080/v1";
    const id = f.querySelector("[name=pid]"); if (!id.value) id.value = e.target.value === "compatible" ? "local" : e.target.value;
    f.querySelectorAll("[data-only-type]").forEach((el) => { el.hidden = el.dataset.onlyType !== e.target.value; });
  }
});
document.querySelectorAll("form").forEach((f) => { if (f.querySelector("[data-compat-harness]")) syncCompat(f); });


// Channel form: only display (and send) fields of the chosen type.
function syncChannel(form) {
  const typ = form.querySelector("[name=type]:not([type=hidden])")?.value || form.querySelector("input[type=hidden][name=type]")?.value;
  // Grayed out examples of the identifier and name: those of the chosen type (not always those from Teams).
  const ex = { webhook: ["webhook-ci", T("CI webhook")], teams: ["teams-dev", T("Dev team (Teams)")],
               jira: ["jira-project", T("Project Jira")], smtp: ["team-mail", T("Team e-mail")] }[typ];
  const id = form.querySelector("#c-id:not([disabled])"), nm = form.querySelector("#c-name");
  if (ex && id) id.placeholder = ex[0];
  if (nm) nm.placeholder = T("Notification channel name");
  form.querySelectorAll("[data-for]").forEach((d) => {
    const on = d.dataset.for === typ;
    d.hidden = !on;
    d.querySelectorAll("input, select").forEach((i) => { i.disabled = !on; });
  });
}
document.addEventListener("change", (e) => { if (e.target.matches("[data-channel-form] [name=type]")) syncChannel(e.target.form); });
document.querySelectorAll("[data-channel-form]").forEach(syncChannel);

// ---- Archives: search while typing, multiple selection, deletion confirmed by typing ----
(function () {
  var form = document.querySelector("[data-arch-search]");
  if (!form) return;
  var timer = null, seq = 0;
  function params() {
    var p = new URLSearchParams(new FormData(form));
    Array.from(p.keys()).forEach(function (k) { if (!p.get(k)) p.delete(k); });
    return p;
  }
  function search() {
    var p = params(), mine = ++seq, qs = p.toString();
    p.set("partial", "1");
    fetch("/archives?" + p.toString(), { credentials: "same-origin", headers: { "Accept": "text/html" } })
      .then(function (r) { return r.ok ? r.text() : null; })
      .then(function (html) {
        if (html === null || mine !== seq) return;   // an older response does not overwrite the most recent one
        var box = document.getElementById("arch-list");
        if (box) box.outerHTML = html;
        history.replaceState(null, "", "/archives" + (qs ? "?" + qs : ""));
      }).catch(function () {});
  }
  form.addEventListener("input", function (e) {
    clearTimeout(timer);
    timer = setTimeout(search, e.target.type === "search" ? 250 : 0);
  });
  form.addEventListener("submit", function (e) { e.preventDefault(); clearTimeout(timer); search(); });

  function checked() { return Array.from(document.querySelectorAll('#arch-list input[name="ref"]:checked')); }
  function syncBulk() {
    var bar = document.querySelector("[data-bulk]"), n = checked().length;
    if (!bar) return;
    bar.hidden = n === 0;
    bar.querySelector("[data-bulk-n]").textContent = n;
  }
  document.addEventListener("change", function (e) {
    if (e.target.matches("[data-check-all]")) {
      document.querySelectorAll('#arch-list input[name="ref"]:not(:disabled)').forEach(function (c) { c.checked = e.target.checked; });
    }
    if (e.target.closest("#arch-list")) syncBulk();
  });

  // "Open in large" from Archives: the current search follows, to return to it as is.
  document.addEventListener("click", function (e) {
    var x = e.target.closest("#panel a[data-expand]");
    if (!x) return;
    var q = new URLSearchParams(location.search); q.delete("job");
    if (q.toString()) x.href = x.pathname + "?back=" + encodeURIComponent(q.toString());
  });
  // Line title: same effect as clicking its reference (opens the side panel).
  document.addEventListener("click", function (e) {
    var t = e.target.closest("[data-row-proxy]");
    if (!t || e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
    var a = document.querySelector('a[data-row][data-ref="' + t.dataset.rowProxy + '"]');
    if (a) { e.preventDefault(); a.click(); }
  });
})();

// ---- Permanent deletion of an archived job: window present on all admin pages ----
(function () {
  var dlg = document.getElementById("del-dlg");
  if (!dlg) return;
  var dform = dlg.querySelector("[data-delete-form]"), typed = dlg.querySelector("#del-typed"), go = dlg.querySelector("[data-delete-go]");
  var phraseEl = dlg.querySelector("[data-delete-phrase]"), what = dlg.querySelector("[data-delete-what]"), refsBox = dlg.querySelector("[data-delete-refs]");
  var phrase = "", loose = false;
  function open(action, text, wanted, refs, caseless) {
    dform.action = action; what.textContent = text; phrase = wanted; loose = !!caseless;
    phraseEl.textContent = wanted; typed.value = ""; go.disabled = true; refsBox.textContent = "";
    (refs || []).forEach(function (r) {
      var i = document.createElement("input"); i.type = "hidden"; i.name = "ref"; i.value = r; refsBox.appendChild(i);
    });
    if (refs) { var a = document.createElement("input"); a.type = "hidden"; a.name = "action"; a.value = "delete"; refsBox.appendChild(a); }
    dlg.showModal(); typed.focus();
  }
  typed.addEventListener("input", function () {
    var v = typed.value.trim();
    go.disabled = loose ? v.toLowerCase() !== phrase.toLowerCase() : v !== phrase;
  });
  document.addEventListener("click", function (e) {
    var one = e.target.closest("[data-delete-ref]");
    if (one) {
      open("/jobs/" + encodeURIComponent(one.dataset.deleteRef) + "/delete",
           T("Delete {ref}: “{title}”?").replace("{ref}", one.dataset.deleteRef).replace("{title}", one.dataset.deleteTitle), one.dataset.deletePhrase, null, false);
      return;
    }
    if (e.target.closest("[data-bulk-delete]")) {
      var refs = Array.from(document.querySelectorAll('#arch-list input[name="ref"]:checked')).map(function (c) { return c.value; });
      if (refs.length) open("/archives/bulk", "Supprimer " + refs.length + " job(s) : " + refs.join(", ") + " ?", "supprimer " + refs.length + " jobs", refs, true);
    }
  });
})();
