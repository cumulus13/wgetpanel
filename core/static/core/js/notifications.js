(function () {
  "use strict";
  var W = window.WGP, h = W.h, $ = function (id) { return document.getElementById(id); };
  var state = null;

  function banner(text, ok) {
    var b = $("notify-msg");
    b.textContent = text;
    b.className = "mb-4 rounded-lg border p-3 text-sm " + (ok ? "border-emerald-300 bg-emerald-50 text-emerald-900 dark:border-emerald-800 dark:bg-emerald-950 dark:text-emerald-200"
      : "border-red-300 bg-red-50 text-red-900 dark:border-red-800 dark:bg-red-950 dark:text-red-200");
  }

  function render(d) {
    state = d;
    $("n-enabled").checked = d.enabled;
    $("n-global").textContent = d.global_scope === "all" ? "The server administrator also receives a copy of every user's events on the server channel." : "";
    $("ntfy-unavailable").classList.toggle("hidden", d.ntfy.available);
    $("ntfy-body").classList.toggle("hidden", !d.ntfy.available);
    $("n-ntfy").checked = d.ntfy.enabled;
    $("ntfy-topic-box").classList.toggle("hidden", !(d.ntfy.enabled && d.ntfy.topic));
    $("n-topic-url").textContent = d.ntfy.subscribe_url;
    $("n-topic").textContent = d.ntfy.topic;
    $("n-ntfy-server").textContent = d.ntfy.subscribe_url ? d.ntfy.subscribe_url.slice(0, d.ntfy.subscribe_url.length - d.ntfy.topic.length - 1) : "";
    $("gntp-unavailable").classList.toggle("hidden", d.gntp.available);
    $("gntp-body").classList.toggle("hidden", !d.gntp.available);
    $("n-gntp").checked = d.gntp.enabled;
    $("n-gntp-host").value = d.gntp.host; $("n-gntp-port").value = d.gntp.port;
    $("n-gntp-pass").value = ""; $("n-gntp-clear").checked = false;
    $("n-gntp-has").textContent = d.gntp.has_password ? "(a password is set)" : "";
    $("n-gntp-nets").textContent = d.gntp.allowed_networks.join(", ");
    var useDefault = d.events === null;
    $("n-events-default").checked = useDefault;
    $("n-events-def").textContent = d.defaults.events.join(", ") || "none";
    var box = $("n-events");
    box.replaceChildren.apply(box, d.known_events.map(function (e) {
      var on = (d.events || d.defaults.events).indexOf(e) >= 0;
      return h("label", { class: "flex items-center gap-1" }, [h("input", { type: "checkbox", value: e, checked: on, disabled: useDefault }), e]);
    }));
    $("n-interval").value = d.retry_interval === null ? "" : d.retry_interval;
    $("n-interval-def").textContent = d.defaults.retry_interval;
  }

  function collect() {
    var body = { enabled: $("n-enabled").checked };
    if (state.ntfy.available) body.ntfy_enabled = $("n-ntfy").checked;
    if (state.gntp.available) {
      body.gntp_host = $("n-gntp-host").value.trim();
      body.gntp_port = parseInt($("n-gntp-port").value, 10) || 23053;
      if ($("n-gntp-pass").value) body.gntp_password = $("n-gntp-pass").value;
      if ($("n-gntp-clear").checked) body.clear_gntp_password = true;
      body.gntp_enabled = $("n-gntp").checked;
    }
    body.events = $("n-events-default").checked ? null : Array.from($("n-events").querySelectorAll("input:checked")).map(function (i) { return i.value; });
    var iv = $("n-interval").value.trim();
    body.retry_interval = iv === "" ? null : parseInt(iv, 10);
    return body;
  }

  async function save(extra) {
    var r = await W.api("/api/notify/", Object.assign(collect(), extra || {}));
    if (!r) return null;
    if (!r.ok) { banner(r.data.error || "Could not save", false); return null; }
    render(r.data);
    return r.data;
  }

  $("n-events-default").addEventListener("change", function (e) {
    $("n-events").querySelectorAll("input").forEach(function (i) { i.disabled = e.target.checked; });
  });
  $("form-notify").addEventListener("submit", async function (e) {
    e.preventDefault();
    if (await save()) banner("Saved.", true);
  });
  $("btn-rotate").addEventListener("click", async function () {
    if (confirm("The old topic stops working. Subscribe to the new one in the ntfy app afterwards. Continue?") && await save({ rotate_topic: true })) banner("New topic generated.", true);
  });
  $("btn-test").addEventListener("click", async function () {
    if (!(await save())) return;
    var r = await W.api("/api/notify/test/", { target: "me" });
    if (!r) return;
    if (!r.ok) { banner(r.data.error || "Test failed", false); return; }
    var parts = Object.keys(r.data.results).map(function (k) { return k + ": " + r.data.results[k]; });
    banner("Test sent. " + parts.join("; "), Object.keys(r.data.results).every(function (k) { return r.data.results[k] === "ok"; }));
  });
  W.api("/api/notify/").then(function (r) { if (r && r.ok) render(r.data); });
})();
