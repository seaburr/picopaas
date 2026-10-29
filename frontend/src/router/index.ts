import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '../stores/auth'

const router = createRouter({
  history: createWebHistory('/'),
  scrollBehavior(to, _from, saved) {
    if (to.hash) return { el: to.hash, top: window.innerWidth < 768 ? 64 : 8 } // mobile: clear the sticky guide bar
    return saved || { top: 0 }
  },
  routes: [
    { path: '/guide', redirect: '/guide/user' },
    { path: '/guide/admin', component: () => import('../views/GuideView.vue'), meta: { title: 'Admin guide', requiresAuth: true, requiresAdmin: true } },
    { path: '/guide/:doc(user|api|cli|recipes)', component: () => import('../views/GuideView.vue'), meta: { title: 'Docs' } }, // public, no auth
    { path: '/pending', component: () => import('../views/AccountStatusView.vue'), meta: { requiresAuth: true, title: 'Account status' } },
    { path: '/login', component: () => import('../views/LoginView.vue'), meta: { title: 'Log in' } },
    { path: '/register', component: () => import('../views/RegisterView.vue'), meta: { title: 'Request access' } },
    { path: '/', component: { render: () => null }, meta: { requiresAuth: true } }, // always redirected in beforeEach
    { path: '/account', component: () => import('../views/AccountView.vue'), meta: { requiresAuth: true, title: 'Account' } },
    { path: '/apps', component: () => import('../views/AppsListView.vue'), meta: { requiresAuth: true, title: 'Apps' } },
    {
      path: '/apps/new',
      component: () => import('../views/AppFormView.vue'),
      meta: { requiresAuth: true, title: 'New app' },
    },
    {
      path: '/apps/:id(\\d+)',
      component: () => import('../views/AppDetailView.vue'),
      meta: { requiresAuth: true, title: 'App' },
      props: true,
    },
    {
      path: '/apps/:id/edit',
      component: () => import('../views/AppFormView.vue'),
      meta: { requiresAuth: true, title: 'App settings' },
      props: true,
    },
    { path: '/apps/:id(\\d+)/settings', redirect: (to) => `/apps/${to.params.id}/edit` },
    { path: '/projects/new', component: () => import('../views/ProjectView.vue'), meta: { requiresAuth: true, title: 'New project' } },
    { path: '/projects/:id(\\d+)', component: () => import('../views/ProjectView.vue'), meta: { requiresAuth: true, title: 'Project' }, props: true },
    { path: '/invite/:token', component: () => import('../views/InviteView.vue'), meta: { requiresAuth: true, title: 'Project invite' }, props: true },
    { path: '/templates', component: () => import('../views/TemplatesView.vue'), meta: { requiresAuth: true, title: 'Templates' } },
    { path: '/keys', redirect: '/account#api-keys' },
    { path: '/api-keys', redirect: '/account#api-keys' },
    {
      path: '/admin',
      component: () => import('../views/AdminView.vue'),
      meta: { requiresAuth: true, requiresAdmin: true, title: 'Admin' },
    },
    {
      path: '/admin/health',
      component: () => import('../views/HealthView.vue'),
      meta: { requiresAuth: true, requiresAdmin: true, title: 'Health' },
    },
    // Unknown paths (public, no auth): a 404 page instead of an empty shell.
    { path: '/:pathMatch(.*)*', component: () => import('../views/NotFoundView.vue'), meta: { title: 'Page not found' } },
  ],
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()
  if (!auth.loaded) await auth.load()
  if (to.path.startsWith('/guide') && !to.meta.requiresAuth) return
  if (to.meta.requiresAuth && !auth.user) {
    return to.fullPath === '/' ? '/login' : { path: '/login', query: { next: to.fullPath } }
  }
  if (auth.user && auth.user.status !== 'active') {
    return to.path === '/pending' ? undefined : '/pending'
  }
  // Everyone lands on /apps, except an admin on a fresh platform (no DB servers yet) who lands on the setup checklist.
  const home = auth.user?.role === 'platform_admin' && auth.user.features?.databases === false ? '/admin' : '/apps'
  if (to.path === '/') return home
  if (to.path === '/pending') return home
  if (to.meta.requiresAdmin && auth.user?.role !== 'platform_admin') return '/apps'
  if ((to.path === '/login' || to.path === '/register') && auth.user) return home
})

// Per-route document.title, e.g. "Apps · PicoPaaS". A view can override it by setting document.title itself after mount.
router.afterEach((to) => {
  const t = to.meta.title as string | undefined
  document.title = t ? `${t} · PicoPaaS` : 'PicoPaaS'
})

export default router
