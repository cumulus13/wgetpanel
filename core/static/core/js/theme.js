(function () {
  var stored = null;
  try { stored = localStorage.getItem("theme"); } catch (e) {}
  var def = document.documentElement.getAttribute("data-default-theme") || "auto";
  var mode = stored || def;
  var dark = mode === "dark" || (mode === "auto" && window.matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.classList.toggle("dark", dark);
})();
