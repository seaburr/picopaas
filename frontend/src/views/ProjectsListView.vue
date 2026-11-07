<script setup lang="ts">
import { onMounted } from 'vue'
import { useRouter } from 'vue-router'
import IconButton from '../components/IconButton.vue'
import { useAuthStore } from '../stores/auth'
import { useProjectsStore } from '../stores/projects'
import type { Project } from '../types'
import { projectApps, projectKaisen } from '../utils/paths'

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

function open(p: Project, to: 'apps' | 'kaisen') {
  router.push(to === 'apps' ? projectApps(p.id) : projectKaisen(p.id))
}
</script>

<template>
  <main id="main" tabindex="-1" class="page-main">
    <div class="mb-3 flex flex-wrap items-center justify-between gap-3">
      <h1 class="page-title">Projects</h1>
      <RouterLink to="/projects/new" class="btn-primary btn-sm">New project</RouterLink>
    </div>

    <div v-if="projects.loadError" role="alert" class="alert-error mb-4 flex flex-wrap items-center justify-between gap-2">
      <span>{{ projects.loadError }}</span>
      <IconButton text="Retry" label="Retry" @click="projects.load()" />
    </div>
    <div v-else-if="!projects.loaded" class="text-sm text-zinc-400" aria-busy="true">Loading…</div>
    <p v-else-if="projects.projects.length === 0" class="meta">No projects yet. <RouterLink to="/projects/new" class="link">Create one</RouterLink>.</p>

    <template v-else>
      <!-- 760px and up: table -->
      <table class="tbl max-[759px]:hidden">
        <colgroup>
          <col />
          <col class="w-28" />
          <col class="w-24" />
          <col class="w-28" />
          <col class="w-44" />
        </colgroup>
        <thead>
          <tr>
            <th scope="col">Project</th>
            <th scope="col">Your role</th>
            <th scope="col">Members</th>
            <th scope="col">Apps</th>
            <th scope="col" class="text-right">Open</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="p in projects.projects" :key="p.id">
            <td class="truncate">
              <RouterLink :to="`/projects/${p.id}`" class="font-medium text-white hover:underline" :title="name(p)">{{ name(p) }}</RouterLink>
              <span v-if="p.is_personal" class="meta ml-2">just you</span>
              <span v-if="suspended(p)" class="st tone-warn ml-2 text-xs"><span class="dot dot-warn" aria-hidden="true" />Suspended</span>
            </td>
            <td class="text-zinc-300">{{ roleLabel(p) }}</td>
            <td class="mono text-zinc-300">{{ p.member_count }}</td>
            <td class="mono">{{ p.app_count }} of {{ p.app_limit_effective }}</td>
            <td class="text-right">
              <div class="row-acts">
                <button type="button" class="row-act max-[759px]:!min-h-[44px]" :aria-label="`Apps in ${name(p)}`" @click="open(p, 'apps')">Apps</button>
                <button v-if="auth.user?.features?.kaisen" type="button" class="row-act max-[759px]:!min-h-[44px]" :aria-label="`Kaisen builds in ${name(p)}`" @click="open(p, 'kaisen')">Kaisen</button>
              </div>
            </td>
          </tr>
        </tbody>
      </table>

      <!-- below 760px: divider-separated rows -->
      <ul class="divide-y divide-pico-border border-y border-pico-border min-[760px]:hidden">
        <li v-for="p in projects.projects" :key="p.id" class="py-2">
          <div class="flex items-start justify-between gap-2">
            <RouterLink :to="`/projects/${p.id}`" class="flex min-h-[36px] min-w-0 items-center break-all font-medium text-white hover:underline">{{ name(p) }}</RouterLink>
            <span class="meta shrink-0 pt-2">{{ roleLabel(p) }}</span>
          </div>
          <p class="meta">
            {{ members(p) }} · <span class="mono">{{ p.app_count }} of {{ p.app_limit_effective }}</span> apps<template v-if="p.is_personal"> · just you</template>
            <span v-if="suspended(p)" class="st tone-warn ml-1"><span class="dot dot-warn" aria-hidden="true" />Suspended</span>
          </p>
          <div class="row-acts !justify-start gap-x-4">
            <button type="button" class="row-act max-[759px]:!min-h-[44px]" :aria-label="`Apps in ${name(p)}`" @click="open(p, 'apps')">Apps</button>
            <button v-if="auth.user?.features?.kaisen" type="button" class="row-act max-[759px]:!min-h-[44px]" :aria-label="`Kaisen builds in ${name(p)}`" @click="open(p, 'kaisen')">Kaisen</button>
          </div>
        </li>
      </ul>
    </template>
  </main>
</template>
