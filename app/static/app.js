// Fabrique : panneau latéral dynamique, raccourcis clavier, temps réel (SSE), toasts.
// Tout fonctionne aussi sans JavaScript (liens et formulaires classiques) ; ce script ne fait qu'accélérer.
(function () {
  "use strict";
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  var store = {
    get: function (k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set: function (k, v) { try { localStorage.setItem(k, v); } catch (e) {} }
  };
  var page = document.body.dataset.page;

  // ---- Thème et barre latérale ----
  function markTheme() {
    var cur = document.documentElement.getAttribute("data-theme-choice") || "dark";
    $$("[data-theme-set]").forEach(function (b) { b.setAttribute("aria-checked", b.dataset.themeSet === cur ? "true" : "false"); });
  }
  $$("[data-theme-set]").forEach(function (b) {
    b.addEventListener("click", function () { store.set("fabrique-theme", b.dataset.themeSet); window.fabriqueTheme(b.dataset.themeSet); markTheme(); });
  });
  markTheme();
  // Menu utilisateur : se referme quand on clique ailleurs ou sur Échap
  document.addEventListener("click", function (e) { $$("details.usermenu[open]").forEach(function (m) { if (!m.contains(e.target)) m.open = false; }); });
  function toggleSide() {
    // Petit écran : la barre est toujours réduite ; le bouton l'ouvre par-dessus la page, avec ses libellés.
    if (window.matchMedia("(max-width: 900px)").matches) { document.documentElement.classList.toggle("side-open"); return; }
    var on = document.documentElement.classList.toggle("side-min");
    store.set("fabrique-side", on ? "min" : "full");
  }
  document.addEventListener("click", function (e) {      // un appui hors de la barre ouverte la referme
    var h = document.documentElement;
    if (h.classList.contains("side-open") && !e.target.closest(".side") && !e.target.closest("[data-sidebar-toggle]")) h.classList.remove("side-open");
  });
  $$("[data-sidebar-toggle]").forEach(function (b) { b.addEventListener("click", toggleSide); });

  // Confirmation avant une action destructive
  // Confirmation (délégation) : le rafraîchissement en direct peut remplacer le formulaire pendant que la question est
  // ouverte ; un formulaire détaché n'est plus envoyé par le navigateur. On envoie donc une copie, rattachée à la page.
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

  // Sélecteurs qui s'envoient seuls
  $$("[data-autosubmit]").forEach(function (s) { s.addEventListener("change", function () { s.form.submit(); }); });

  // ---- Boîtes de dialogue ----
  $$("[data-open-dialog]").forEach(function (b) {
    b.addEventListener("click", function () {
      $$("details.usermenu[open]").forEach(function (m) { m.open = false; });   // le menu se referme derrière la fenêtre
      var d = document.getElementById(b.dataset.openDialog); if (d) d.showModal();
    });
  });
  $$("[data-close-dialog]").forEach(function (b) { b.addEventListener("click", function () { b.closest("dialog").close(); }); });
  // Un clic en dehors d'une fenêtre modale (sur le fond) la ferme.
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

  // ---- Ctrl+↵ (ou Cmd+↵) envoie le formulaire de réponse ----
  document.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      var f = e.target.closest && e.target.closest("form[data-cmd-enter]");
      if (f) { e.preventDefault(); f.requestSubmit(); }
    }
  });

  // ---- Panneau latéral (jobs, boîte de réception) ----
  var panel = $("#panel");
  var kind = panel && panel.dataset.kind;
  var openRef = (function () {
    var q = new URLSearchParams(location.search);
    return q.get("job") || q.get("item") || (panel && panel.dataset.open) || null;
  })();

  // Inbox : le premier élément est ouvert d'office, sauf sur petit écran où le volet recouvrirait la liste.
  if (panel && panel.classList.contains("auto")) {
    if (window.matchMedia("(max-width: 900px)").matches) { openRef = null; panel.hidden = true; panel.innerHTML = ""; }
    panel.classList.remove("auto");
  }
  // Connexion OIDC : essai de la découverte sans recharger la page (sinon la saisie non enregistrée serait perdue).
  var oidcTest = $("[data-oidc-test]");
  if (oidcTest) oidcTest.addEventListener("click", function () {
    var form = oidcTest.closest("form"), out = $("[data-oidc-test-out]", form);
    out.hidden = false; out.className = "git-test muted"; out.textContent = "Test en cours…"; oidcTest.disabled = true;
    fetch("/settings-test/oidc", { method: "POST", body: new FormData(form), credentials: "same-origin", headers: { "Accept": "application/json" } })
      .then(function (r) { return r.json(); })
      .then(function (d) { out.className = "git-test " + (d.ok ? "ok" : "err"); out.textContent = d.msg; })
      .catch(function () { out.className = "git-test err"; out.textContent = "Le test n'a pas abouti : recharge la page et réessaie."; })
      .then(function () { oidcTest.disabled = false; });
  });
  // Provider Git : essai du token avant d'enregistrer (rien n'est écrit côté serveur).
  var gitTest = $("[data-git-test]");
  if (gitTest) gitTest.addEventListener("click", function () {
    var form = gitTest.closest("form"), out = $("[data-git-test-out]", form);
    out.hidden = false; out.className = "git-test muted"; out.textContent = "Test en cours…"; gitTest.disabled = true;
    fetch("/settings-test/git", { method: "POST", body: new FormData(form), credentials: "same-origin", headers: { "Accept": "application/json" } })
      .then(function (r) { return r.json(); })
      .then(function (d) { out.className = "git-test " + (d.ok ? "ok" : "err"); out.textContent = d.msg; })
      .catch(function () { out.className = "git-test err"; out.textContent = "Le test n'a pas abouti : recharge la page et réessaie."; })
      .then(function () { gitTest.disabled = false; });
  });
  // Nouvelle demande : libellés et exemples selon le type choisi (changement ou étude).
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
  // Nouvelle demande : le résumé de ce qui s'appliquera suit le repo choisi.
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
  // État à préserver quand une zone est remplacée : défilements et sections <details> ouvertes ou fermées.
  function keyOf(d, i) { var s = d.querySelector("summary"); return (d.dataset.key || (s ? s.textContent.trim().slice(0, 80) : "")) + "#" + i; }
  function saveState(root) {
    var st = { scroll: [], open: {} };
    $$(".board, .col-b, .fcol, .p-body, .list, .events", root).forEach(function (el, i) { st.scroll.push([i, el.scrollLeft, el.scrollTop]); });
    if (root.scrollTop || root.scrollLeft) st.self = [root.scrollLeft, root.scrollTop];
    $$("details", root).forEach(function (d, i) { st.open[keyOf(d, i)] = d.open; });
    return st;
  }
  function restoreState(root, st) {
    var els = $$(".board, .col-b, .fcol, .p-body, .list, .events", root);
    st.scroll.forEach(function (x) { var el = els[x[0]]; if (el) { el.scrollLeft = x[1]; el.scrollTop = x[2]; } });
    if (st.self) { root.scrollLeft = st.self[0]; root.scrollTop = st.self[1]; }
    $$("details", root).forEach(function (d, i) { var k = keyOf(d, i); if (k in st.open) d.open = st.open[k]; });
  }

  function loadPanel(ref, push) {
    if (!panel || (kind !== "job" && kind !== "inbox")) return false;
    return fetch(panelUrl(ref), { headers: { "Accept": "text/html" }, credentials: "same-origin" }).then(function (r) {
      if (r.status === 401) { location.reload(); return; }
      if (!r.ok) { if (push) location.href = pageUrl(ref); return; }
      return r.text().then(function (html) {
        var same = openRef === ref && !push;
        var typing = panel.contains(document.activeElement) && /TEXTAREA|INPUT/.test(document.activeElement.tagName) && document.activeElement.value;
        if (!push && typing) return;   // ne jamais écraser une réponse en cours de saisie
        var st = same ? saveState(panel) : null;
        panel.innerHTML = html; panel.hidden = false; wire(panel);
        if (st) restoreState(panel, st);
        openRef = ref; markSelected(ref);
        if (push) history.pushState({ ref: ref }, "", pageUrl(ref));
      });
    });
  }
  function closePanel(push) {
    if (!panel) return;
    panel.hidden = true; panel.innerHTML = ""; openRef = null; markSelected(null);
    if (push) history.pushState({ ref: null }, "", pageUrl(null));
  }
  function wire(root) {
    $$("[data-close]", root).forEach(function (a) { a.addEventListener("click", function (e) { e.preventDefault(); closePanel(true); }); });
    $$("[data-cmd-enter] textarea", root).forEach(function (t) { if (t.hasAttribute("autofocus")) t.focus(); });
  }
  if (panel) wire(panel);
  document.addEventListener("click", function (e) {
    var a = e.target.closest && e.target.closest("a[data-row][data-ref]");
    if (!a || !panel || e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
    e.preventDefault(); loadPanel(a.dataset.ref, true);
  });
  window.addEventListener("popstate", function () {
    var q = new URLSearchParams(location.search), ref = q.get("job") || q.get("item");
    if (ref) loadPanel(ref, false); else closePanel(false);
  });

  // ---- Raccourcis : J/K naviguer, ↵ ouvrir, Échap fermer, [ barre latérale ----
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

  // ---- Temps réel : à chaque changement en base, on rafraîchit les zones [data-live] et le panneau ouvert ----
  var live = $("#live");
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
          if (el.innerHTML === fresh.innerHTML) return;   // rien n'a changé : on ne touche à rien
          var st = saveState(el), main = $("#main"), mainScroll = main ? [main.scrollLeft, main.scrollTop] : null;
          var focusRef = document.activeElement && document.activeElement.dataset ? document.activeElement.dataset.ref : null;
          el.innerHTML = fresh.innerHTML;
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
  // Temps réel : UNE seule connexion ouverte en permanence, quel que soit le nombre d'onglets. Derrière un intermédiaire en
  // HTTP/1.1, un navigateur n'ouvre que 6 connexions par site : une connexion par onglet, plus celle d'une page quittée qui
  // tarde à se fermer, suffisaient à bloquer la page suivante. Un onglet « meneur » (verrou Web Locks) tient la connexion et
  // relaie les changements aux autres (BroadcastChannel) ; elle est fermée dès que la page est quittée.
  if (window.EventSource && live) {
    var es = null, bc = null, leader = false;
    var onChange = function () { clearTimeout(pending); pending = setTimeout(refresh, 250); };
    var setState = function (s) { live.dataset.state = s; if (leader && bc) bc.postMessage(s); };
    var open = function () {
      if (es) return;
      es = new EventSource("/events/stream");
      es.addEventListener("open", function () { setState("on"); });
      es.addEventListener("change", function () { onChange(); if (bc) bc.postMessage("change"); });
      es.addEventListener("error", function () { setState("off"); });
    };
    var stop = function () { if (es) { es.close(); es = null; } };
    var start = function () {
      if (window.BroadcastChannel && navigator.locks) {
        bc = new BroadcastChannel("fabrique-live");
        bc.onmessage = function (m) {
          if (m.data === "change") onChange();
          else if (!leader && (m.data === "on" || m.data === "off")) live.dataset.state = m.data;
        };
        live.dataset.state = "on";                       // relayé par l'onglet meneur, en attendant de le devenir
        navigator.locks.request("fabrique-live", function () {
          leader = true; open();
          return new Promise(function (done) { window.addEventListener("pagehide", done, { once: true }); });
        });
      } else {
        open();                                          // navigateur sans ces API (ou page non sécurisée) : une connexion par onglet
      }
    };
    // La connexion est fermée AVANT que la page suivante soit demandée, pour lui laisser la place.
    window.addEventListener("beforeunload", stop);
    window.addEventListener("pagehide", stop);
    window.addEventListener("pageshow", function (e) { if (e.persisted) location.reload(); });
    if (document.readyState === "complete") start(); else window.addEventListener("load", start, { once: true });
  }
})();


// Copier le journal des événements (bouton aux deux carrés) : lignes dans l'ordre chronologique, texte brut.
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
  b.title = ok ? "Copié" : "Copie impossible";
  setTimeout(() => { b.innerHTML = old; b.title = "Copier le journal (ordre chronologique)"; }, 1500);
});


// Agent ↔ modèle : un agent ne propose que les modèles qui parlent son API ; type de fournisseur → adresse préremplie.
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
    const b = f.querySelector("[name=base_url]"); b.value = base; b.placeholder = base || "http://serveur-modele:8080/v1";
    const id = f.querySelector("[name=pid]"); if (!id.value) id.value = e.target.value === "compatible" ? "local" : e.target.value;
    f.querySelectorAll("[data-only-type]").forEach((el) => { el.hidden = el.dataset.onlyType !== e.target.value; });
  }
});
document.querySelectorAll("form").forEach((f) => { if (f.querySelector("[data-compat-harness]")) syncCompat(f); });


// Formulaire de canal : n'afficher (et n'envoyer) que les champs du type choisi.
function syncChannel(form) {
  const typ = form.querySelector("[name=type]:not([type=hidden])")?.value || form.querySelector("input[type=hidden][name=type]")?.value;
  // Exemples grisés de l'identifiant et du nom : ceux du type choisi (pas toujours ceux de Teams).
  const ex = { webhook: ["webhook-ci", "Webhook de la CI"], teams: ["teams-equipe", "Équipe dev (Teams)"],
               jira: ["jira-projet", "Jira du projet"], smtp: ["mail-equipe", "E-mail de l'équipe"] }[typ];
  const id = form.querySelector("#c-id:not([disabled])"), nm = form.querySelector("#c-name");
  if (ex && id) id.placeholder = ex[0];
  if (nm) nm.placeholder = "Nom canal de notification";
  form.querySelectorAll("[data-for]").forEach((d) => {
    const on = d.dataset.for === typ;
    d.hidden = !on;
    d.querySelectorAll("input, select").forEach((i) => { i.disabled = !on; });
  });
}
document.addEventListener("change", (e) => { if (e.target.matches("[data-channel-form] [name=type]")) syncChannel(e.target.form); });
document.querySelectorAll("[data-channel-form]").forEach(syncChannel);

// ---- Archives : recherche pendant la frappe, sélection multiple, suppression confirmée par saisie ----
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
        if (html === null || mine !== seq) return;   // une réponse plus ancienne n'écrase pas la plus récente
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

  // « Ouvrir en grand » depuis les Archives : la recherche en cours suit, pour y revenir telle quelle.
  document.addEventListener("click", function (e) {
    var x = e.target.closest("#panel a[data-expand]");
    if (!x) return;
    var q = new URLSearchParams(location.search); q.delete("job");
    if (q.toString()) x.href = x.pathname + "?back=" + encodeURIComponent(q.toString());
  });
  // Titre d'une ligne : même effet qu'un clic sur sa référence (ouvre le panneau latéral).
  document.addEventListener("click", function (e) {
    var t = e.target.closest("[data-row-proxy]");
    if (!t || e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
    var a = document.querySelector('a[data-row][data-ref="' + t.dataset.rowProxy + '"]');
    if (a) { e.preventDefault(); a.click(); }
  });
})();

// ---- Suppression définitive d'un job archivé : fenêtre présente sur toutes les pages d'un admin ----
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
           "Supprimer " + one.dataset.deleteRef + " : « " + one.dataset.deleteTitle + " » ?", one.dataset.deletePhrase, null, false);
      return;
    }
    if (e.target.closest("[data-bulk-delete]")) {
      var refs = Array.from(document.querySelectorAll('#arch-list input[name="ref"]:checked')).map(function (c) { return c.value; });
      if (refs.length) open("/archives/bulk", "Supprimer " + refs.length + " job(s) : " + refs.join(", ") + " ?", "supprimer " + refs.length + " jobs", refs, true);
    }
  });
})();
