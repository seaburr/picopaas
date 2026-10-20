import { createPinia } from 'pinia'
import { createApp } from 'vue'
import App from './App.vue'
import { setUnauthorizedHandler } from './api/client'
import router from './router'
import { useAuthStore } from './stores/auth'
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
