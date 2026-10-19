(function () {
  "use strict";
  var W = window.WGP;
  document.getElementById("form-settings").addEventListener("submit", async function (e) {
    e.preventDefault();
    var body = {};
    document.querySelectorAll("[data-key]").forEach(function (el) {
      body[el.dataset.key] = el.dataset.type === "bool" ? el.checked : el.value;
    });
    document.querySelectorAll("[data-err]").forEach(function (p) { p.classList.add("hidden"); });
    var r = await W.api("/api/settings/", body);
    if (r.ok) { W.toast("Settings saved"); return; }
    var errs = r.data.errors || {};
    Object.keys(errs).forEach(function (k) {
      var p = document.querySelector('[data-err="' + k + '"]'); if (p) { p.textContent = errs[k]; p.classList.remove("hidden"); }
    });
    W.toast("Some values were rejected, the rest was saved", "error");
  });
})();
