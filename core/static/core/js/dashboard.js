(function () {
  "use strict";
  var W = window.WGP, h = W.h;
  var POLL = parseInt((document.querySelector('meta[name="poll-ms"]') || {}).content, 10) || 1000;
  var IS_STAFF = !!document.querySelector('a[href="/settings/"]');
  var STATUS = {
    running: "bg-blue-100 text-blue-800 dark:bg-blue-950 dark:text-blue-300",
    queued: "bg-slate-200 text-slate-700 dark:bg-slate-800 dark:text-slate-300",
    paused: "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300",
    completed: "bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300",
    error: "bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-300",
    canceled: "bg-slate-200 text-slate-500 dark:bg-slate-800 dark:text-slate-400"
  };
  var FILTERS = [["all", "All"], ["running", "Running"], ["queued", "Queued"], ["paused", "Paused"], ["completed", "Completed"], ["error", "Failed"], ["canceled", "Canceled"]];
  var state = { filter: "all", selected: new Set(), rows: new Map(), last: null, detailId: null };

  // ---- charts
  function chartColors() {
    var dark = document.documentElement.classList.contains("dark");
    return { line: "#6366f1", fill: dark ? "rgba(99,102,241,.25)" : "rgba(99,102,241,.15)", grid: dark ? "#1e293b" : "#e2e8f0", text: dark ? "#94a3b8" : "#64748b" };
  }
  function makeChart(canvas) {
    var c = chartColors();
    return new Chart(canvas, {
      type: "line",
      data: { labels: [], datasets: [{ data: [], borderColor: c.line, backgroundColor: c.fill, fill: true, tension: 0.3, pointRadius: 0, borderWidth: 2 }] },
      options: {
        animation: false, responsive: true, maintainAspectRatio: false, interaction: { intersect: false, mode: "index" },
        plugins: { legend: { display: false }, tooltip: { callbacks: { label: function (x) { return W.rate(x.parsed.y); } } } },
        scales: {
          x: { ticks: { color: c.text, maxTicksLimit: 6 }, grid: { display: false } },
          y: { beginAtZero: true, ticks: { color: c.text, callback: function (v) { return W.rate(v); } }, grid: { color: c.grid } }
        }
      }
    });
  }
  function feed(chart, series) {
    chart.data.labels = series.map(function (p) { return new Date(p.t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }); });
    chart.data.datasets[0].data = series.map(function (p) { return p.speed; });
    chart.update("none");
  }
  var speedChart = makeChart(document.getElementById("speed-chart"));
  var detailChart = null;
  document.addEventListener("themechange", function () {
    [speedChart, detailChart].forEach(function (ch) {
      if (!ch) return;
      var c = chartColors();
      ch.data.datasets[0].borderColor = c.line; ch.data.datasets[0].backgroundColor = c.fill;
      ch.options.scales.x.ticks.color = c.text; ch.options.scales.y.ticks.color = c.text; ch.options.scales.y.grid.color = c.grid;
      ch.update("none");
    });
  });

  // ---- filters
  var filtersEl = document.getElementById("filters");
  function renderFilters(counts) {
    var btns = FILTERS.map(function (f) {
      var n = f[0] === "all" ? Object.keys(counts).reduce(function (a, k) { return a + counts[k]; }, 0) : counts[f[0]] || 0;
      var active = state.filter === f[0];
      return h("button", {
        type: "button", class: "rounded-md px-3 py-1 " + (active ? "bg-indigo-600 text-white" : "hover:bg-slate-100 dark:hover:bg-slate-800"),
        text: f[1] + " (" + n + ")", onclick: function () { state.filter = f[0]; render(); }
      });
    });
    filtersEl.replaceChildren.apply(filtersEl, btns);
  }

  // ---- rows
  function actionBtn(label, title, fn, danger) {
    return h("button", { type: "button", class: "btn-icon" + (danger ? " text-red-600" : ""), title: title, "aria-label": title, text: label, onclick: fn });
  }
  function act(action, ids, extra) {
    return W.api("/api/action/", Object.assign({ action: action, ids: ids }, extra || {})).then(function (r) {
      if (r && !r.ok) W.toast(r.data.error || "Action failed", "error");
      poll();
    });
  }
  function buildRow(d) {
    var chk = h("input", { type: "checkbox", "aria-label": "Select", onchange: function (e) { if (e.target.checked) state.selected.add(d.id); else state.selected.delete(d.id); updateBulk(); } });
    var name = h("div", { class: "max-w-md truncate font-medium" });
    var url = h("div", { class: "max-w-md truncate text-xs text-slate-500" });
    var owner = h("div", { class: "text-xs text-slate-400" });
    var badge = h("span", { class: "badge" });
    var err = h("div", { class: "mt-1 max-w-xs truncate text-xs text-red-600" });
    var bar = h("div", { class: "h-full rounded-full bg-indigo-600 transition-all" });
    var track = h("div", { class: "h-2 w-full overflow-hidden rounded-full bg-slate-200 dark:bg-slate-700" }, [bar]);
    var prog = h("div", { class: "mt-1 text-xs text-slate-500 tabular-nums" });
    var speed = h("td", { class: "px-3 py-3 tabular-nums" });
    var eta = h("td", { class: "px-3 py-3 tabular-nums" });
    var actions = h("td", { class: "whitespace-nowrap px-3 py-3 text-right" });
    var tr = h("tr", { class: "hover:bg-slate-50 dark:hover:bg-slate-800/40" }, [
      h("td", { class: "px-3 py-3" }, [chk]), h("td", { class: "px-3 py-3" }, [name, url, owner]),
      h("td", { class: "px-3 py-3" }, [badge, err]), h("td", { class: "px-3 py-3" }, [track, prog]), speed, eta, actions
    ]);
    return { tr: tr, chk: chk, owner: owner, badge: badge, err: err, bar: bar, prog: prog, speed: speed, eta: eta, actions: actions, name: name, url: url, sig: "" };
  }
  function updateRow(r, d) {
    r.name.textContent = d.filename; r.name.title = d.filename; r.url.textContent = d.url; r.url.title = d.url;
    r.owner.textContent = d.owner && IS_STAFF ? "by " + d.owner : "";
    r.badge.textContent = d.status; r.badge.className = "badge " + STATUS[d.status];
    r.err.textContent = d.error ? d.error.split("\n")[0] : ""; r.err.title = d.error || "";
    var pct = d.status === "completed" ? 100 : d.progress;
    r.bar.style.width = (pct == null ? (d.status === "running" ? 8 : 0) : pct) + "%";
    r.bar.className = "h-full rounded-full transition-all " + (d.status === "error" ? "bg-red-500" : d.status === "completed" ? "bg-emerald-500" : "bg-indigo-600") + (pct == null && d.status === "running" ? " animate-pulse" : "");
    r.prog.textContent = (pct == null ? "" : pct + "% · ") + W.bytes(d.downloaded) + (d.total ? " / " + W.bytes(d.total) : "");
    r.speed.textContent = d.status === "running" ? W.rate(d.speed) : "–";
    r.eta.textContent = d.status === "running" ? W.eta(d.eta) : "–";
    r.chk.checked = state.selected.has(d.id);
    if (r.sig !== d.status) {
      r.sig = d.status;
      var a = [];
      if (d.status === "running" || d.status === "queued") a.push(actionBtn("⏸", "Pause", function () { act("pause", [d.id]); }));
      if (d.status === "paused") a.push(actionBtn("▶", "Resume", function () { act("resume", [d.id]); }));
      if (d.status === "error" || d.status === "canceled") a.push(actionBtn("↻", "Retry", function () { act("retry", [d.id]); }));
      if (d.status === "completed") a.push(h("a", { class: "btn-icon inline-block", href: "/api/download/" + d.id + "/file/", title: "Download file", "aria-label": "Download file", text: "⬇" }));
      a.push(actionBtn("ⓘ", "Details", function () { openDetail(d.id); }));
      if (d.status === "running" || d.status === "queued" || d.status === "paused") {
        a.push(actionBtn("✕", "Cancel", function () {
          if (confirm("Cancel this download?")) act("cancel", [d.id], { delete_files: confirm("Also delete the partial file?") });
        }, true));
      } else {
        a.push(actionBtn("🗑", "Remove from list", function () { act("remove", [d.id]); }, true));
      }
      r.actions.replaceChildren.apply(r.actions, a);
    }
  }

  function render() {
    var s = state.last;
    if (!s) return;
    renderFilters(s.counts);
    var list = s.downloads.filter(function (d) { return state.filter === "all" || d.status === state.filter; });
    var tbody = document.getElementById("rows"), seen = new Set();
    list.forEach(function (d) {
      var r = state.rows.get(d.id);
      if (!r) { r = buildRow(d); state.rows.set(d.id, r); }
      updateRow(r, d); tbody.appendChild(r.tr); seen.add(d.id);
    });
    state.rows.forEach(function (r, id) { if (!seen.has(id)) { r.tr.remove(); state.rows.delete(id); state.selected.delete(id); } });
    document.getElementById("empty").classList.toggle("hidden", list.length > 0);
    updateBulk();
  }
  function updateBulk() {
    document.querySelectorAll("[data-bulk]").forEach(function (b) { b.disabled = state.selected.size === 0; });
    var all = document.getElementById("chk-all"), n = state.rows.size;
    all.checked = n > 0 && state.selected.size === n;
  }

  // ---- top cards & banners
  function renderTop(s) {
    document.getElementById("st-speed").textContent = W.rate(s.speed);
    document.getElementById("st-running").textContent = s.counts.running;
    document.getElementById("st-slots").textContent = s.max_downloads ? "of " + s.max_downloads + " slots" : "no limit";
    document.getElementById("st-queued").textContent = s.counts.queued;
    document.getElementById("st-completed").textContent = s.counts.completed;
    document.getElementById("st-error").textContent = s.counts.error;
    feed(speedChart, s.series);
    var b = document.getElementById("banners"), msgs = [];
    if (!s.redis) msgs.push("Redis is unreachable: live speed and pause/cancel of running downloads are degraded.");
    if (!s.scheduler_alive && (s.counts.queued || s.counts.running)) msgs.push("The scheduler is not running (no tick in the last 30 s). Start celery beat and the workers, otherwise queued downloads will not start.");
    b.replaceChildren.apply(b, msgs.map(function (m) {
      return h("div", { class: "rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-200", text: m });
    }));
  }

  // ---- polling
  var timer = null, busy = false;
  async function poll() {
    if (busy) return;
    busy = true;
    try {
      var r = await W.api("/api/snapshot/");
      if (r && r.ok) { state.last = r.data; renderTop(r.data); render(); if (state.detailId) await refreshDetail(); }
    } catch (e) { /* network blip: keep polling */ }
    busy = false;
  }
  function loop() {
    clearTimeout(timer);
    timer = setTimeout(async function () { await poll(); loop(); }, document.hidden ? Math.max(POLL, 5000) : POLL);
  }

  // ---- detail dialog
  var dlgDetail = document.getElementById("dlg-detail");
  function openDetail(id) {
    state.detailId = id;
    if (!detailChart) detailChart = makeChart(document.getElementById("detail-chart"));
    dlgDetail.showModal();
    refreshDetail();
  }
  dlgDetail.addEventListener("close", function () { state.detailId = null; });
  async function refreshDetail() {
    if (!state.detailId) return;
    var r = await W.api("/api/download/" + state.detailId + "/");
    if (!r || !r.ok) return;
    var d = r.data.download, meta = document.getElementById("d-meta");
    document.getElementById("d-title").textContent = d.filename;
    var rows = [["Status", d.status], ["URL", d.url], ["Directory", d.directory], ["Size", d.total ? W.bytes(d.total) : "unknown"],
      ["Downloaded", W.bytes(d.downloaded)], ["Speed", d.status === "running" ? W.rate(d.speed) : "–"],
      ["Exit code", r.data.exit_code == null ? "–" : String(r.data.exit_code)], ["Restarts after worker loss", String(r.data.requeues)]];
    if (d.error) rows.push(["Error", d.error]);
    var nodes = [];
    rows.forEach(function (x) {
      nodes.push(h("dt", { class: "text-slate-500", text: x[0] }));
      nodes.push(h("dd", { class: "break-all whitespace-pre-wrap", text: x[1] }));
    });
    meta.replaceChildren.apply(meta, nodes);
    feed(detailChart, r.data.series);
    var log = document.getElementById("d-log");
    var atEnd = log.scrollTop + log.clientHeight >= log.scrollHeight - 20;
    log.textContent = r.data.log || "(no log yet)";
    if (atEnd) log.scrollTop = log.scrollHeight;
  }

  // ---- add dialog
  var dlgAdd = document.getElementById("dlg-add");
  document.getElementById("btn-add").addEventListener("click", function () {
    document.getElementById("add-errors").classList.add("hidden");
    dlgAdd.showModal();
    document.getElementById("f-urls").focus();
  });
  document.getElementById("form-add").addEventListener("submit", async function (e) {
    e.preventDefault();
    var v = function (id) { return document.getElementById(id).value.trim(); };
    var opts = {};
    if (v("f-rate")) opts.limit_rate = parseInt(v("f-rate"), 10) * 1024;
    if (v("f-tries")) opts.tries = parseInt(v("f-tries"), 10);
    if (v("f-ua")) opts.user_agent = v("f-ua");
    if (v("f-ref")) opts.referer = v("f-ref");
    if (v("f-headers")) opts.headers = v("f-headers");
    if (document.getElementById("f-nocert").checked) opts.check_certificate = false;
    var body = {
      urls: document.getElementById("f-urls").value, directory: v("f-dir"), filename: v("f-name"), options: opts,
      http_user: v("f-user"), http_password: document.getElementById("f-pass").value
    };
    if (v("f-start")) body.not_before = new Date(v("f-start")).toISOString();
    var r = await W.api("/api/add/", body), box = document.getElementById("add-errors");
    if (!r) return;
    var errs = r.data.errors || [];
    if (errs.length) {
      box.classList.remove("hidden");
      box.replaceChildren.apply(box, errs.map(function (er) { return h("div", { text: (er.line ? "Line " + er.line + ": " : "") + er.error }); }));
    } else {
      box.classList.add("hidden");
    }
    if (r.ok) {
      W.toast(r.data.created.length + " download(s) added");
      if (!errs.length) { dlgAdd.close(); document.getElementById("form-add").reset(); }
      else document.getElementById("f-urls").value = errs.map(function (er) { return er.url; }).join("\n");
      poll();
    }
  });

  // ---- bulk / purge / select all
  document.querySelectorAll("[data-bulk]").forEach(function (b) {
    b.addEventListener("click", function () {
      var ids = Array.from(state.selected), a = b.dataset.bulk, extra = {};
      if ((a === "remove" || a === "cancel") && !confirm(a + " " + ids.length + " download(s)?")) return;
      if (a === "remove" || a === "cancel") extra.delete_files = confirm("Also delete the downloaded / partial files from disk?");
      act(a, ids, extra).then(function () { if (a === "remove") state.selected.clear(); });
    });
  });
  document.getElementById("btn-purge").addEventListener("click", function () {
    if (confirm("Remove all finished downloads from the list? (files stay on disk)")) act("purge", []);
  });
  document.getElementById("chk-all").addEventListener("change", function (e) {
    state.rows.forEach(function (r, id) {
      if (e.target.checked) state.selected.add(id); else state.selected.delete(id);
      r.chk.checked = e.target.checked;
    });
    updateBulk();
  });
  document.addEventListener("visibilitychange", loop);

  poll().then(loop);
})();
