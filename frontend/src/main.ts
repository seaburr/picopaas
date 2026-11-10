import { createPinia } from 'pinia'
import { createApp } from 'vue'
import App from './App.vue'
import { setUnauthorizedHandler } from './api/client'
import router from './router'
import { useAuthStore } from './stores/auth'
import '@fontsource-variable/inter/wght.css'
import '@fontsource-variable/jetbrains-mono/wght.css'
import './style.css'

const pinia = createPinia()
const app = createApp(App).use(pinia).use(router)

// A 401 from a non-/auth call = session expired: clear auth, go to /login?next=<current page>.
setUnauthorizedHandler(() => {
  const auth = useAuthStore(pinia)
  if (!auth.user) return
  auth.user = null
  const cur = router.currentRoute.value
  if (cur.meta.requiresAuth) router.push({ path: '/login', query: { next: cur.fullPath } })
})

// After a deploy, an open tab still references the previous build's hashed chunks, which no longer exist, so lazy
// routes fail to load. Do a full page load of the target once (picks up the new index.html); the timestamp guard
// stops a reload loop if the chunk is genuinely broken.
const RELOAD_KEY = 'picopaas-chunk-reload'
// (Only route components are lazy, so router.onError sees every stale-chunk failure, with the intended target.)
const STALE_CHUNK = /Failed to fetch dynamically imported module|Importing a module script failed|error loading dynamically imported module|Unable to preload CSS/i
router.onError((err, to) => {
  if (!STALE_CHUNK.test(String(err?.message ?? err))) return
  let last = 0
  try { last = Number(sessionStorage.getItem(RELOAD_KEY) || 0) } catch { /* storage blocked */ }
  if (Date.now() - last < 10_000) return
  try { sessionStorage.setItem(RELOAD_KEY, String(Date.now())) } catch { /* storage blocked */ }
  window.location.assign(to.fullPath)
})

app.mount('#app')

// Once the first page is up and the browser is idle, fetch the other lazy page chunks (the ones a signed-in user
// reaches from the sidebar) so the first click on each is not a network round trip. Errors are ignored: the real
// navigation retries and router.onError above handles a stale chunk.
const idle = (fn: () => void) => ('requestIdleCallback' in window ? window.requestIdleCallback(fn, { timeout: 4000 }) : setTimeout(fn, 1500))
router.isReady().then(() => {
  if (!useAuthStore(pinia).user) return
  idle(() => {
    for (const load of [
      () => import('./views/AppDetailView.vue'),
      () => import('./views/AppFormView.vue'),
      () => import('./views/AppsListView.vue'),
      () => import('./views/TemplatesView.vue'),
      () => import('./views/ProjectsListView.vue'),
      () => import('./views/AccountView.vue'),
    ]) void load().catch(() => {})
  })
})
