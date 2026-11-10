<script setup lang="ts">
import type { Banner } from '../api/admin'

// The platform notice bar. Title bold, then text, both plain text. info and warning share one slim bar.
defineProps<{ banner: Pick<Banner, 'level' | 'title' | 'text'>; slim?: boolean }>()
</script>

<template>
  <div
    role="note"
    :class="[banner.level === 'info' ? 'alert-info' : 'alert-warn', slim ? 'rounded-none border-x-0 border-t-0 !border-b-2 px-5 py-1.5 text-center text-sm leading-relaxed' : '']"
  >
    <strong v-if="banner.title">{{ banner.title }}</strong>{{ banner.title && banner.text ? ' ' : '' }}{{ banner.text }}
  </div>
</template>
