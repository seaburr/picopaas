document.getElementById("js-check").textContent = "JS OK";
fetch("/api/info")
  .then(function (r) { return r.json(); })
  .then(function (d) { document.getElementById("info").textContent = d.hostname + " (" + d.version + ")"; })
  .catch(function () { document.getElementById("info").textContent = "error"; })
  .finally(function () { document.body.setAttribute("data-ok", "1"); });
