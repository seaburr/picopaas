<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { listUsers } from '../api/admin'
import { statusLook } from '../composables/statusTone'
import { useAppsStore } from '../stores/apps'
import { useAuthStore } from '../stores/auth'
import { useProjectsStore } from '../stores/projects'
import { useSidebarStore } from '../stores/sidebar'
import type { Project } from '../types'
import SideLink from './SideLink.vue'

// The signed-in shell: wordmark, Projects > project > app tree, Kaisen, Templates, Docs, Administration, account foot.
// Below 760px it is a top bar with a Menu button that opens the same tree full screen.
// Tree nodes are native <details>: the triangle toggles, the name navigates. Open/closed is remembered per node in
// localStorage (only what the user chose); the path to the current page is revealed on every navigation.
const auth = useAuthStore()
const projects = useProjectsStore()
const apps = useAppsStore()
const side = useSidebarStore()
const route = useRoute()
const router = useRouter()

const isAdmin = computed(() => auth.user?.role === 'platform_admin')
const signedIn = computed(() => !!auth.user)

// ---- pending approvals (admin) ----
const pending = ref(0)
async function refreshPending() {
  if (!isAdmin.value) return
  try {
    pending.value = ((await listUsers()) ?? []).filter((u) => u.status === 'pending').length
  } catch {
    pending.value = 0
  }
}

// ---- open/closed state ----
const KEY = 'picopaas.nav'
function readUser(): Record<string, boolean> {
  try {
    const v = JSON.parse(localStorage.getItem(KEY) ?? '{}')
    return v && typeof v === 'object' ? v : {}
  } catch {
    return {}
  }
}
const user = ref<Record<string, boolean>>(readUser())
function persist() {
  try {
    localStorage.setItem(KEY, JSON.stringify(user.value))
  } catch {
    // storage blocked: the choice lasts for this tab only
  }
}
const pathKeys = ref<Set<string>>(new Set())
const DEFAULT_OPEN = new Set(['projects', 'admin'])
const isOpen = (key: string) => user.value[key] ?? (pathKeys.value.has(key) || DEFAULT_OPEN.has(key))
function onToggle(key: string, e: Event) {
  const open = (e.target as HTMLDetailsElement).open
  if (open === isOpen(key)) return // the initial toggle event of a node rendered open
  user.value = { ...user.value, [key]: open }
  persist()
  if (open && key.startsWith('p:')) ensureLoaded()
}

// ---- current location ----
const appMatch = computed(() => /^\/apps\/(\d+)(\/edit)?$/.exec(route.path))
const routeAppId = computed(() => (appMatch.value ? Number(appMatch.value[1]) : null))
const routeAppProject = ref<number | null>(null)
const currentProjectId = computed(() => projects.current?.id ?? null)

async function resolveAppProject() {
  const id = routeAppId.value
  routeAppProject.value = null
  if (id === null || !signedIn.value) return
  const hit = side.find(id) ?? apps.apps.find((a) => a.id === id)
  if (hit?.project_id) {
    routeAppProject.value = hit.project_id
    return
  }
  try {
    const a = await side.fetchOne(id)
    if (routeAppId.value === id && a?.project_id) routeAppProject.value = a.project_id
  } catch {
    // the page itself shows the error; the tree just stays collapsed
  }
}

function reveal() {
  const keys = new Set<string>()
  const p = route.path
  if (p.startsWith('/kaisen')) keys.add('kaisen')
  if (p.startsWith('/admin')) keys.add('admin')
  if (p.startsWith('/guide')) keys.add('docs')
  if (p === '/apps' || p === '/apps/new') {
    keys.add('projects')
    if (currentProjectId.value) keys.add(`p:${currentProjectId.value}`)
  }
  if (routeAppId.value !== null) {
    keys.add('projects')
    keys.add(`a:${routeAppId.value}`)
    const pid = routeAppProject.value ?? currentProjectId.value
    if (pid) keys.add(`p:${pid}`)
  }
  pathKeys.value = keys
  // Navigating to something reveals it: drop an earlier explicit "closed" on the path.
  const next = { ...user.value }
  let changed = false
  for (const k of keys) {
    if (k in next) {
      delete next[k]
      changed = true
    }
  }
  if (changed) {
    user.value = next
    persist()
  }
}

// ---- apps per project ----
// Current project: the apps store (kept fresh by the Apps page) is written through. Others: lazily, when opened.
watch(
  () => [apps.apps, apps.loaded] as const,
  ([list, loaded]) => {
    const id = currentProjectId.value
    if (!loaded || id === null || !signedIn.value) return
    if (list.length && !list.every((a) => a.project_id === id)) return // still the previous project's list
    side.setFromStore(id, list)
  },
  { deep: false },
)
function ensureLoaded() {
  if (!signedIn.value || !projects.loaded) return
  for (const p of projects.projects) {
    const isCur = p.id === currentProjectId.value
    if (isCur && apps.loaded) continue // reuse the apps store
    if (isCur || isOpen(`p:${p.id}`) || p.id === routeAppProject.value) void side.loadProject(p.id, 15000)
  }
}
const appsFor = (p: Project) => side.byProject[p.id] ?? []
const countFor = (p: Project) => (side.byProject[p.id] ? side.byProject[p.id].length : p.app_count)
const appLabel = (status: string) => `${statusLook(status).label}:`

async function onRoute() {
  menuOpen.value = false
  void refreshPending()
  if (!auth.user) {
    reveal()
    return
  }
  if (!projects.loaded) await projects.load()
  await resolveAppProject()
  reveal()
  ensureLoaded()
}
watch(() => route.fullPath, onRoute)
watch(() => auth.user?.id, onRoute)
watch(currentProjectId, () => {
  reveal()
  ensureLoaded()
})
onMounted(onRoute)

function openProject(p: Project) {
  projects.select(p.id)
  user.value = { ...user.value, [`p:${p.id}`]: true }
  persist()
}
const projectName = (p: Project) => p.name
const curProject = (p: Project) => route.path === '/apps' && p.id === currentProjectId.value

// ---- phone menu ----
const menuOpen = ref(false)
const menuBtn = ref<HTMLButtonElement | null>(null)
const closeBtn = ref<HTMLButtonElement | null>(null)
function openMenu() {
  menuOpen.value = true
  nextTick(() => closeBtn.value?.focus())
}
function closeMenu() {
  if (!menuOpen.value) return
  menuOpen.value = false
  nextTick(() => menuBtn.value?.focus())
}

function onNavClick(e: MouseEvent) {
  if ((e.target as HTMLElement).closest('a')) menuOpen.value = false
}

async function onLogout() {
  menuOpen.value = false
  await auth.logout()
  side.reset()
  router.push('/login')
}

function skip(e: Event) {
  e.preventDefault()
  const el = document.getElementById('main')
  el?.focus()
  el?.scrollIntoView()
}

const DOCS = [
  { to: '/guide/user', title: 'User guide' },
  { to: '/guide/api', title: 'API' },
  { to: '/guide/cli', title: 'CLI' },
  { to: '/guide/recipes', title: 'Recipes' },
]
const path = computed(() => route.path)
const kaisenBuilds = computed(() => path.value === '/kaisen' || path.value === '/kaisen/new' || /^\/kaisen\/\d+/.test(path.value))
</script>

<template>
  <!-- eslint-disable-next-line vuejs-accessibility/no-static-element-interactions -- Escape closes the open phone menu anywhere in the shell nav; the Menu/Close buttons stay the keyboard controls -->
  <div class="shell-nav" @keydown.esc="closeMenu">
    <a href="#main" class="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-[60] focus:rounded-md focus:border focus:bg-pico-bg focus:px-3 focus:py-2 focus:text-sm focus:text-white" @click="skip">
      Skip to content
    </a>

    <header class="side-top">
      <RouterLink :to="signedIn ? '/apps' : '/guide'" class="side-brand" aria-label="PicoPaaS home">PicoPaaS</RouterLink>
      <button
        ref="menuBtn"
        type="button"
        class="btn-secondary"
        :aria-expanded="menuOpen"
        aria-controls="site-nav"
        @click="openMenu"
      >
        Menu
      </button>
    </header>

    <!-- eslint-disable-next-line vuejs-accessibility/no-static-element-interactions, vuejs-accessibility/click-events-have-key-events -- delegated: any link tapped in the full-screen phone menu closes it, even when it points at the current page -->
    <nav id="site-nav" class="side" aria-label="Main" :data-open="menuOpen" @click="onNavClick">
      <div class="side-head">
        <RouterLink :to="signedIn ? '/apps' : '/guide'" class="side-brand">PicoPaaS</RouterLink>
        <button ref="closeBtn" type="button" class="btn-secondary" @click="closeMenu">Close</button>
      </div>
      <RouterLink :to="signedIn ? '/apps' : '/guide'" class="side-brand side-brand-desk" aria-label="PicoPaaS home">PicoPaaS</RouterLink>

      <!-- Signed in -->
      <ul v-if="signedIn" class="tree">
        <li>
          <details class="disc" :open="isOpen('projects')" @toggle="onToggle('projects', $event)">
            <summary><SideLink to="/projects" :cur="path === '/projects'">Projects</SideLink></summary>
            <ul>
              <li v-if="!projects.loaded && !projects.loadError"><p class="sub-note">Loading…</p></li>
              <li v-else-if="projects.loadError"><p class="sub-note">Couldn't load projects. <button type="button" @click="projects.load().then(ensureLoaded)">Retry</button></p></li>
              <li v-for="p in projects.projects" :key="p.id">
                <details class="disc" :open="isOpen(`p:${p.id}`)" @toggle="onToggle(`p:${p.id}`, $event)">
                  <summary>
                    <SideLink to="/apps" :cur="curProject(p)" @go="openProject(p)">{{ projectName(p) }} <span class="n m">{{ countFor(p) }}</span></SideLink>
                  </summary>
                  <ul>
                    <li v-if="side.errors[p.id] && !appsFor(p).length"><p class="sub-note">Couldn't load apps. <button type="button" @click="side.loadProject(p.id)">Retry</button></p></li>
                    <li v-else-if="side.loading[p.id] && !appsFor(p).length"><p class="sub-note" aria-busy="true">Loading…</p></li>
                    <li v-else-if="!appsFor(p).length"><p class="sub-note">No apps</p></li>
                    <li v-for="a in appsFor(p)" :key="a.id">
                      <details v-if="a.id === routeAppId" class="disc" :open="isOpen(`a:${a.id}`)" @toggle="onToggle(`a:${a.id}`, $event)">
                        <summary>
                          <SideLink :to="`/apps/${a.id}`"><span :class="['dot', `dot-${statusLook(a.status).tone}`]" aria-hidden="true" /><span class="sr-only">{{ appLabel(a.status) }}</span><span class="m mono">{{ a.name }}</span></SideLink>
                        </summary>
                        <ul>
                          <li><SideLink :to="`/apps/${a.id}`" :cur="path === `/apps/${a.id}`">Health</SideLink></li>
                          <li><SideLink :to="`/apps/${a.id}/edit`" :cur="path === `/apps/${a.id}/edit`">Settings</SideLink></li>
                        </ul>
                      </details>
                      <SideLink v-else :to="`/apps/${a.id}`"><span :class="['dot', `dot-${statusLook(a.status).tone}`]" aria-hidden="true" /><span class="sr-only">{{ appLabel(a.status) }}</span><span class="m mono">{{ a.name }}</span></SideLink>
                    </li>
                  </ul>
                </details>
              </li>
            </ul>
          </details>
        </li>
        <li v-if="auth.user?.features?.kaisen">
          <details class="disc" :open="isOpen('kaisen')" @toggle="onToggle('kaisen', $event)">
            <summary><SideLink to="/kaisen">Kaisen <span class="tag">Preview</span></SideLink></summary>
            <ul>
              <li><SideLink to="/kaisen" :cur="kaisenBuilds">Builds</SideLink></li>
              <li><SideLink to="/kaisen/settings" :cur="path === '/kaisen/settings'">Settings</SideLink></li>
            </ul>
          </details>
        </li>
        <li><SideLink to="/templates" :cur="path === '/templates'">Templates</SideLink></li>
        <li><SideLink to="/guide" :cur="path.startsWith('/guide')" :exact="false">Docs</SideLink></li>
      </ul>

      <ul v-if="isAdmin" class="tree adm">
        <li>
          <details class="disc" :open="isOpen('admin')" @toggle="onToggle('admin', $event)">
            <summary><span class="lbl">Administration</span></summary>
            <ul>
              <li><SideLink to="/admin" :cur="path === '/admin'">Admin <span v-if="pending > 0" class="n m">{{ pending }} pending</span></SideLink></li>
              <li><SideLink to="/admin/health" :cur="path === '/admin/health'">Health</SideLink></li>
            </ul>
          </details>
        </li>
      </ul>

      <!-- Signed out (public docs): Docs links only -->
      <ul v-if="!signedIn" class="tree">
        <li>
          <details class="disc" :open="isOpen('docs')" @toggle="onToggle('docs', $event)">
            <summary><SideLink to="/guide" :cur="false">Docs</SideLink></summary>
            <ul>
              <li v-for="d in DOCS" :key="d.to"><SideLink :to="d.to" :cur="path === d.to">{{ d.title }}</SideLink></li>
            </ul>
          </details>
        </li>
        <li><a href="/privacy">Privacy</a></li>
      </ul>

      <div class="side-foot">
        <template v-if="signedIn">
          <span class="who" :title="auth.user?.email">{{ auth.user?.email }}</span>
          <span class="links">
            <SideLink to="/account" :cur="path === '/account'">Account</SideLink>
            <button type="button" @click="onLogout">Log out</button>
          </span>
          <span class="links fine">
            <a href="/privacy">Privacy</a>
            <a href="/privacy#terms">Terms</a>
            <a href="/privacy#report-abuse">Report abuse</a>
            <a href="https://blog.picopaas.xyz" target="_blank" rel="noopener">Blog</a>
          </span>
        </template>
        <span v-else class="links">
          <SideLink to="/login">Log in</SideLink>
          <SideLink to="/register">Request access</SideLink>
        </span>
      </div>
    </nav>
  </div>
</template>
