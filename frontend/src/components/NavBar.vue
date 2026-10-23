<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { listUsers } from '../api/admin'
import { useAuthStore } from '../stores/auth'
import BrandMark from './BrandMark.vue'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()
const isAdmin = computed(() => auth.user?.role === 'platform_admin')
const pending = ref(0)
async function refreshPending() {
  if (!isAdmin.value) return
  try {
    pending.value = ((await listUsers()) ?? []).filter((u) => u.status === 'pending').length
  } catch {
    pending.value = 0
  }
}
onMounted(refreshPending)
watch(() => route.fullPath, refreshPending)

const menuOpen = ref(false)
const menuBtn = ref<HTMLButtonElement | null>(null)
watch(() => route.fullPath, () => (menuOpen.value = false))
function closeMenu() {
  if (!menuOpen.value) return
  menuOpen.value = false
  nextTick(() => menuBtn.value?.focus())
}

async function onLogout() {
  menuOpen.value = false
  await auth.logout()
  router.push('/login')
}

function skip(e: Event) {
  e.preventDefault()
  const el = document.getElementById('main')
  el?.focus()
  el?.scrollIntoView()
}
</script>

<template>
  <header class="relative border-b border-pico-border bg-pico-panel" @keydown.esc="closeMenu">
    <a href="#main" class="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:rounded-md focus:bg-pico-card focus:px-3 focus:py-2 focus:text-sm focus:text-white" @click="skip">
      Skip to content
    </a>
    <div class="mx-auto flex max-w-6xl items-center justify-between gap-2 px-4 py-2 sm:px-6">
      <div class="flex min-w-0 items-center gap-1 lg:gap-3">
        <RouterLink to="/" class="mr-1 rounded-md py-1 pr-2" aria-label="PicoPaaS home">
          <BrandMark />
        </RouterLink>

        <nav aria-label="Main" class="hidden items-center gap-0.5 sm:flex">
          <template v-if="auth.user">
            <RouterLink to="/apps" class="nav-link" active-class="nav-active" exact-active-class="nav-active">Apps</RouterLink>
            <RouterLink to="/keys" class="nav-link" active-class="nav-active" exact-active-class="nav-active">API keys</RouterLink>
          </template>
          <RouterLink to="/guide" class="nav-link" active-class="nav-active" exact-active-class="nav-active">Docs</RouterLink>
          <div v-if="isAdmin" class="ml-2 flex items-center gap-0.5 border-l border-pico-border pl-2" role="group" aria-label="Administration">
            <RouterLink to="/admin" class="nav-link" active-class="" exact-active-class="nav-active">
              Admin<span v-if="pending > 0" class="ml-1.5 rounded-full bg-amber-400 px-1.5 text-xs font-semibold text-black" :aria-label="`${pending} pending`">{{ pending }}</span>
            </RouterLink>
            <RouterLink to="/admin/health" class="nav-link" active-class="" exact-active-class="nav-active">Health</RouterLink>
          </div>
        </nav>
      </div>

      <div class="hidden items-center gap-2 sm:flex">
        <RouterLink v-if="auth.user" to="/account" class="nav-link max-w-[16rem]" active-class="nav-active" :title="`${auth.user.email} - account settings`">
          <span class="xl:hidden">Account</span><span class="hidden truncate xl:inline">{{ auth.user.email }}</span>
        </RouterLink>
        <a v-if="!auth.user" href="/privacy" class="nav-link hidden lg:inline-flex">Privacy</a>
        <button v-if="auth.user" type="button" class="btn-secondary btn-sm" @click="onLogout">Log out</button>
        <template v-else>
          <RouterLink to="/login" class="btn-secondary btn-sm">Log in</RouterLink>
          <RouterLink to="/register" class="btn-primary btn-sm">Request access</RouterLink>
        </template>
      </div>

      <button
        ref="menuBtn"
        type="button"
        class="btn-secondary btn-sm sm:hidden"
        :aria-expanded="menuOpen"
        aria-controls="mobile-menu"
        @click="menuOpen = !menuOpen"
      >
        <svg class="h-4 w-4" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true">
          <path v-if="!menuOpen" d="M2 4h12M2 8h12M2 12h12" />
          <path v-else d="M3 3l10 10M13 3L3 13" />
        </svg>
        Menu
      </button>
    </div>

    <!-- Mobile sheet -->
    <nav v-show="menuOpen" id="mobile-menu" aria-label="Main (mobile)" class="absolute inset-x-0 top-full z-40 border-b border-pico-border bg-pico-panel px-4 pb-4 pt-2 shadow-xl sm:hidden">
      <RouterLink v-if="auth.user" to="/account" class="nav-link" active-class="nav-active"><span class="truncate">{{ auth.user.email }}</span><span class="ml-2 shrink-0 text-xs text-zinc-400">Account</span></RouterLink>
      <div class="flex flex-col gap-0.5">
        <template v-if="auth.user">
          <RouterLink to="/apps" class="nav-link" active-class="nav-active" exact-active-class="nav-active">Apps</RouterLink>
          <RouterLink to="/keys" class="nav-link" active-class="nav-active" exact-active-class="nav-active">API keys</RouterLink>
        </template>
        <RouterLink to="/guide" class="nav-link" active-class="nav-active" exact-active-class="nav-active">Docs</RouterLink>
        <a v-if="!auth.user" href="/privacy" class="nav-link">Privacy</a>
        <template v-if="isAdmin">
          <p class="mt-2 px-2.5 text-xs font-medium uppercase tracking-wide text-zinc-400">Administration</p>
          <RouterLink to="/admin" class="nav-link" active-class="" exact-active-class="nav-active">
            Admin<span v-if="pending > 0" class="ml-1.5 rounded-full bg-amber-400 px-1.5 text-xs font-semibold text-black">{{ pending }} pending</span>
          </RouterLink>
          <RouterLink to="/admin/health" class="nav-link" active-class="" exact-active-class="nav-active">Health</RouterLink>
        </template>
      </div>
      <div class="mt-3 flex gap-2 border-t border-pico-border pt-3">
        <button v-if="auth.user" type="button" class="btn-secondary flex-1" @click="onLogout">Log out</button>
        <template v-else>
          <RouterLink to="/login" class="btn-secondary flex-1">Log in</RouterLink>
          <RouterLink to="/register" class="btn-primary flex-1">Request access</RouterLink>
        </template>
      </div>
    </nav>
  </header>
</template>
