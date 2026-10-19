import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '../stores/auth'

const router = createRouter({
  history: createWebHistory('/'),
  routes: [
    { path: '/login', component: () => import('../views/LoginView.vue') },
    { path: '/register', component: () => import('../views/RegisterView.vue') },
    { path: '/', component: () => import('../views/AppsListView.vue'), meta: { requiresAuth: true } },
    {
      path: '/apps/new',
      component: () => import('../views/AppFormView.vue'),
      meta: { requiresAuth: true },
    },
    {
      path: '/apps/:id/edit',
      component: () => import('../views/AppFormView.vue'),
      meta: { requiresAuth: true },
      props: true,
    },
    { path: '/keys', component: () => import('../views/ApiKeysView.vue'), meta: { requiresAuth: true } },
  ],
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()
  if (!auth.loaded) await auth.load()
  if (to.meta.requiresAuth && !auth.user) return '/login'
  if ((to.path === '/login' || to.path === '/register') && auth.user) return '/'
})

export default router
