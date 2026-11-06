// Runs synchronously in <head> (before first paint) so a light-mode user never sees a dark flash.
// Kept as a same-origin file because the app's CSP is script-src 'self' (no inline scripts).
// The Vue app re-applies the server's value after /auth/me (src/utils/theme.ts); keep the key and logic in sync.
(function () {
  var t = 'dark'
  try {
    if (localStorage.getItem('picopaas-theme') === 'light') t = 'light'
  } catch {
    /* storage blocked: default dark */
  }
  var d = document.documentElement
  d.setAttribute('data-theme', t)
  var cs = document.querySelector('meta[name="color-scheme"]')
  var tc = document.querySelector('meta[name="theme-color"]')
  if (cs) cs.setAttribute('content', t)
  if (tc) tc.setAttribute('content', t === 'light' ? '#e7e0d0' : '#0d0d14')
})()
