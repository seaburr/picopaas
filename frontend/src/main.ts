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

app.mount('#app')
