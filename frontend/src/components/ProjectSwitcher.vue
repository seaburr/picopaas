<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { FolderPlus, Settings } from 'lucide-vue-next'
import IconButton from './IconButton.vue'
import { useAuthStore } from '../stores/auth'
import { useProjectsStore } from '../stores/projects'

// Compact project picker, always shown for signed-in users so projects are discoverable (personal-only users get "Personal" + New project).
const auth = useAuthStore()
const projects = useProjectsStore()
const router = useRouter()
onMounted(() => {
  if (auth.user?.status === 'active' && !projects.loaded) projects.load()
})
const show = computed(() => !!auth.user && projects.projects.length > 0)
const current = computed(() => projects.current)

function onPick(e: Event) {
  const id = Number((e.target as HTMLSelectElement).value)
  if (id) projects.select(id)
}
</script>

<template>
  <div v-if="show" class="flex items-center gap-1" role="group" aria-label="Project">
    <select class="input w-auto max-w-40 min-w-0" aria-label="Current project" :value="current?.id" @change="onPick">
      <option v-for="p in projects.projects" :key="p.id" :value="p.id">{{ p.is_personal ? 'Personal' : p.name }}</option>
    </select>
    <IconButton v-if="current && !current.is_personal" :icon="Settings" label="Project settings and members" :to="`/projects/${current.id}`" />
    <IconButton :icon="FolderPlus" label="New project" @click="router.push('/projects/new')" />
  </div>
</template>
