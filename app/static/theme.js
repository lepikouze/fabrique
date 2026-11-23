// Applied before display: theme (light, dark or auto = system; dark by default) and sidebar.
(function () {
  var d = document.documentElement, t = null, s = null;
  try { t = localStorage.getItem("fabrique-theme"); s = localStorage.getItem("fabrique-side"); } catch (e) {}
  var mq = window.matchMedia ? window.matchMedia("(prefers-color-scheme: light)") : null;
  function apply(choice) {
    var eff = choice === "auto" ? (mq && mq.matches ? "light" : "dark") : (choice === "light" ? "light" : "dark");
    d.setAttribute("data-theme", eff);
    d.setAttribute("data-theme-choice", choice === "light" || choice === "auto" ? choice : "dark");
  }
  apply(t);
  if (mq && mq.addEventListener) mq.addEventListener("change", function () { if (d.getAttribute("data-theme-choice") === "auto") apply("auto"); });
  window.fabriqueTheme = apply;
  if (s === "min") d.classList.add("side-min");
})();
