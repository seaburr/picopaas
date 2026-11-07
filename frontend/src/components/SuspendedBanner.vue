<script setup lang="ts">
import type { Project } from '../types'
import { humanDate, fullDate } from '../utils/format'

// Shown first in <main> on every page of a suspended project. The reason arrives only for project owners.
// `kaisen` swaps the Blocked / Still works copy for the Kaisen pages.
defineProps<{ project: Project | null | undefined; kaisen?: boolean }>()
</script>

<template>
  <div v-if="project && project.status === 'suspended'" class="alert-warn mb-3 text-sm" role="status">
    <p class="font-medium">This project is suspended by a platform admin.</p>
    <dl class="mt-1.5 grid grid-cols-1 gap-x-3 gap-y-0.5 sm:grid-cols-[6rem_minmax(0,1fr)]">
      <dt class="font-medium">Blocked</dt>
      <dd class="text-zinc-300">
        <template v-if="kaisen">Kaisen builds and deploys; any run in progress was stopped. Changing settings or members, restarting, restoring backups.</template>
        <template v-else>Changing settings or members, deploying, restarting, restoring backups, and Kaisen builds. Apps are stopped.</template>
      </dd>
      <dt class="font-medium">Still works</dt>
      <dd class="text-zinc-300">
        <template v-if="kaisen">Viewing past builds, their files and diffs; downloading backups.</template>
        <template v-else>Viewing the project, apps, logs and backups; downloading backups.</template>
      </dd>
    </dl>
    <p v-if="project.suspended_reason" class="mt-1.5 wrap-break-word text-zinc-300">
      <strong class="text-white">Reason:</strong> {{ project.suspended_reason }}
      <span v-if="project.suspended_at" class="meta" :title="fullDate(project.suspended_at)">Suspended {{ humanDate(project.suspended_at) }}.</span>
    </p>
    <p v-else class="mt-1.5 text-zinc-400">Ask a project owner or the platform admin why.</p>
  </div>
</template>
