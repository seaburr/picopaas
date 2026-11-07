// Public pages (landing, privacy). Applies a saved theme when this origin has one (the platform host does; the apex does not),
// otherwise the CSS follows the system setting. A same-origin file because the CSP is script-src 'self'. Key matches theme-init.js.
(function () {
  try {
    var t = localStorage.getItem('picopaas-theme')
    if (t === 'light' || t === 'dark') document.documentElement.setAttribute('data-theme', t)
  } catch {
    /* storage blocked: follow the system */
  }
})()
