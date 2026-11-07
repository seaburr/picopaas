// Public pages (landing, privacy). Dark by default, like the app. Applies a saved choice when this origin has one (the
// platform host does; the apex does not). The system colour scheme is deliberately ignored. A same-origin file because
// the CSP is script-src 'self'. Key matches theme-init.js.
(function () {
  try {
    var t = localStorage.getItem('picopaas-theme')
    document.documentElement.setAttribute('data-theme', t === 'light' ? 'light' : 'dark')
  } catch {
    document.documentElement.setAttribute('data-theme', 'dark')
  }
})()
