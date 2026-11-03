<script setup lang="ts">
import { onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { RotateCw } from 'lucide-vue-next'
import NavBar from '../components/NavBar.vue'
import IconButton from '../components/IconButton.vue'
import { useAuthStore } from '../stores/auth'
import { useProjectsStore } from '../stores/projects'
import type { Project } from '../types'

const auth = useAuthStore()
const projects = useProjectsStore()
const router = useRouter()
onMounted(() => {
  if (!projects.loaded) projects.load()
})

const roleLabel = (p: Project) => (p.role === 'owner' ? 'Owner' : 'Member')
const members = (p: Project) => `${p.member_count} ${p.member_count === 1 ? 'member' : 'members'}`
const name = (p: Project) => (p.is_personal ? 'Personal' : p.name)
const suspended = (p: Project) => p.status === 'suspended'

function open(p: Project, to: '/apps' | '/kaisen') {
  projects.select(p.id)
  router.push(to)
}
</script>

<template>
  <div class="min-h-screen bg-pico-bg">
    <NavBar />
    <main id="main" tabindex="-1" class="page-main">
      <div class="mb-3 flex flex-wrap items-center justify-between gap-3">
        <h1 class="page-title">Projects</h1>
        <RouterLink to="/projects/new" class="btn-primary btn-sm">New project</RouterLink>
      </div>

      <div v-if="projects.loadError" role="alert" class="alert-error mb-4 flex flex-wrap items-center justify-between gap-2">
        <span>{{ projects.loadError }}</span>
        <IconButton :icon="RotateCw" label="Retry" @click="projects.load()" />
      </div>
      <div v-else-if="!projects.loaded" class="text-sm text-zinc-400" aria-busy="true">Loading…</div>

      <template v-else>
        <!-- sm and up: table -->
        <div class="card hidden overflow-hidden sm:block">
          <table class="w-full text-left text-sm">
            <thead class="border-b border-pico-border text-xs uppercase tracking-wide text-zinc-400">
              <tr>
                <th scope="col" class="px-3 py-2 font-medium">Project</th>
                <th scope="col" class="px-3 py-2 font-medium">Your role</th>
                <th scope="col" class="px-3 py-2 font-medium">Members</th>
                <th scope="col" class="px-3 py-2 font-medium">Apps</th>
                <th scope="col" class="px-3 py-2 text-right font-medium">Open</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="p in projects.projects" :key="p.id" class="border-b border-pico-border last:border-0">
                <td class="px-3 py-2">
                  <RouterLink :to="`/projects/${p.id}`" class="break-all font-medium text-white hover:underline">{{ name(p) }}</RouterLink>
                  <span v-if="p.is_personal" class="badge-neutral ml-2">just you</span>
                  <span v-if="suspended(p)" class="badge-warn ml-2">Suspended</span>
                </td>
                <td class="px-3 py-2 text-zinc-300">{{ roleLabel(p) }}</td>
                <td class="px-3 py-2 text-zinc-300">{{ p.member_count }}</td>
                <td class="px-3 py-2">{{ p.app_count }} of {{ p.app_limit_effective }}</td>
                <td class="px-3 py-2 text-right">
                  <button type="button" class="link" :aria-label="`Apps in ${name(p)}`" @click="open(p, '/apps')">Apps</button>
                  <template v-if="auth.user?.features?.kaisen">
                    <span aria-hidden="true"> · </span>
                    <button type="button" class="link" :aria-label="`Kaisen builds in ${name(p)}`" @click="open(p, '/kaisen')">Kaisen</button>
                  </template>
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <!-- below sm: stacked cards -->
        <ul class="space-y-2 sm:hidden">
          <li v-for="p in projects.projects" :key="p.id" class="card card-pad">
            <div class="flex items-start justify-between gap-2">
              <RouterLink :to="`/projects/${p.id}`" class="min-w-0 break-all font-semibold text-white hover:underline">{{ name(p) }}</RouterLink>
              <span class="badge-neutral shrink-0">{{ roleLabel(p) }}</span>
            </div>
            <p class="meta mt-1">
              {{ members(p) }} · {{ p.app_count }} of {{ p.app_limit_effective }} apps<template v-if="p.is_personal"> · just you</template>
              <span v-if="suspended(p)" class="badge-warn ml-1">Suspended</span>
            </p>
            <div class="mt-3 flex gap-2">
              <button type="button" class="btn-secondary btn-sm flex-1" @click="open(p, '/apps')">Apps</button>
              <button v-if="auth.user?.features?.kaisen" type="button" class="btn-secondary btn-sm flex-1" @click="open(p, '/kaisen')">Kaisen</button>
            </div>
          </li>
        </ul>
      </template>
    </main>
  </div>
</template>
