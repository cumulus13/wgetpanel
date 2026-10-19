/* Shared helpers. Everything user controlled is inserted with textContent, never innerHTML. */
(function () {
  "use strict";
  var W = (window.WGP = {});
  var csrf = (document.querySelector('meta[name="csrf-token"]') || {}).content || "";

  W.api = async function (url, body) {
    var opt = { credentials: "same-origin", headers: { Accept: "application/json" } };
    if (body !== undefined) {
      opt.method = "POST";
      opt.headers["Content-Type"] = "application/json";
      opt.headers["X-CSRFToken"] = csrf;
      opt.body = JSON.stringify(body);
    }
    var res = await fetch(url, opt);
    if (res.status === 401) { location.href = "/login/?next=" + encodeURIComponent(location.pathname); return null; }
    var data = null;
    try { data = await res.json(); } catch (e) {}
    return { ok: res.ok, status: res.status, data: data || {} };
  };

  W.h = function (tag, attrs, children) {
    var el = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      var v = attrs[k];
      if (v === false || v == null) return;
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else if (k.slice(0, 2) === "on") el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? "" : v);
    });
    (children || []).forEach(function (c) { if (c != null) el.appendChild(typeof c === "string" ? document.createTextNode(c) : c); });
    return el;
  };

  W.toast = function (msg, kind) {
    var box = document.getElementById("toasts");
    var cls = kind === "error" ? "bg-red-600" : "bg-slate-800";
    var t = W.h("div", { class: "max-w-sm rounded-lg px-4 py-2 text-sm text-white shadow-lg " + cls, text: msg });
    box.appendChild(t);
    setTimeout(function () { t.remove(); }, kind === "error" ? 7000 : 3000);
  };

  W.bytes = function (n) {
    if (n == null) return "–";
    var u = ["B", "KB", "MB", "GB", "TB"], i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (i === 0 ? n.toFixed(0) : n.toFixed(n >= 100 ? 0 : 1)) + " " + u[i];
  };
  W.rate = function (n) { return W.bytes(n) + "/s"; };
  W.eta = function (s) {
    if (s == null) return "–";
    if (s < 60) return s + "s";
    if (s < 3600) return Math.floor(s / 60) + "m " + (s % 60) + "s";
    if (s < 86400) return Math.floor(s / 3600) + "h " + Math.floor((s % 3600) / 60) + "m";
    return Math.floor(s / 86400) + "d " + Math.floor((s % 86400) / 3600) + "h";
  };

  var tg = document.getElementById("theme-toggle");
  if (tg) tg.addEventListener("click", function () {
    var dark = document.documentElement.classList.toggle("dark");
    try { localStorage.setItem("theme", dark ? "dark" : "light"); } catch (e) {}
    document.dispatchEvent(new CustomEvent("themechange"));
  });

  document.querySelectorAll("dialog [data-close]").forEach(function (b) {
    b.addEventListener("click", function () { b.closest("dialog").close(); });
  });
})();
