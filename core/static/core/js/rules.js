(function () {
  "use strict";
  var W = window.WGP, h = W.h;
  var DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  var daysBox = document.getElementById("r-days");
  DAYS.forEach(function (d, i) {
    daysBox.appendChild(h("label", { class: "flex items-center gap-1" }, [h("input", { type: "checkbox", value: String(i) }), d]));
  });
  function render(rules) {
    var tb = document.getElementById("rule-rows");
    tb.replaceChildren.apply(tb, rules.map(function (r) {
      var days = r.days ? r.days.split(",").map(function (x) { return DAYS[+x]; }).join(", ") : "every day";
      return h("tr", {}, [
        h("td", { class: "px-3 py-2" }, [h("input", { type: "checkbox", checked: r.enabled, "aria-label": "Enabled", onchange: function (e) { save(Object.assign({}, r, { enabled: e.target.checked })); } })]),
        h("td", { class: "px-3 py-2 font-medium" }, [r.name, r.active_now ? h("span", { class: "badge ml-2 bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300", text: "active now" }) : null]),
        h("td", { class: "px-3 py-2", text: days }), h("td", { class: "px-3 py-2", text: r.start }), h("td", { class: "px-3 py-2", text: r.end }),
        h("td", { class: "px-3 py-2", text: r.limit_kbps ? String(r.limit_kbps) : "unlimited" }),
        h("td", { class: "px-3 py-2 text-right" }, [h("button", { class: "btn btn-danger", type: "button", text: "Delete", onclick: function () { if (confirm("Delete rule?")) save({ delete: r.id }); } })])
      ]);
    }));
    if (!rules.length) tb.appendChild(h("tr", {}, [h("td", { colspan: "7", class: "p-6 text-center text-slate-500", text: "No rules yet." })]));
  }
  async function save(body) {
    var r = await W.api("/api/rules/", body);
    if (!r.ok) { W.toast(r.data.error || "Failed", "error"); return; }
    render(r.data.rules);
  }
  document.getElementById("form-rule").addEventListener("submit", function (e) {
    e.preventDefault();
    var days = Array.from(daysBox.querySelectorAll("input:checked")).map(function (i) { return i.value; }).join(",");
    save({ name: document.getElementById("r-name").value, start: document.getElementById("r-start").value, end: document.getElementById("r-end").value, limit_kbps: document.getElementById("r-limit").value, days: days });
    e.target.reset(); daysBox.querySelectorAll("input").forEach(function (i) { i.checked = false; });
  });
  W.api("/api/rules/").then(function (r) { if (r && r.ok) render(r.data.rules); });
})();
