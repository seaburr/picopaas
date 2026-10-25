<script setup lang="ts">
import type { Component } from 'vue'

// Square icon-only button. Renders a <RouterLink> when `to` is set, an <a> when `href` is set, else a <button>.
// `label` is both the aria-label and the tooltip. Danger turns red on hover/focus only.
const props = defineProps<{
  icon: Component
  label: string
  to?: string
  href?: string
  danger?: boolean
  sm?: boolean
  disabled?: boolean
  expanded?: boolean
  pressed?: boolean
  busy?: boolean // spinning icon, disabled
}>()
defineEmits<{ click: [e: MouseEvent] }>()
const cls = () => ['btn-icon', props.busy ? 'btn-icon-busy' : '', props.sm ? 'btn-icon-sm' : '', props.danger ? 'btn-icon-danger' : '']
</script>

<template>
  <RouterLink v-if="to && !disabled" :to="to" :class="cls()" :title="label" :aria-label="label">
    <component :is="icon" :size="16" :stroke-width="2" aria-hidden="true" />
  </RouterLink>
  <a
    v-else-if="href && !disabled"
    :href="href"
    target="_blank"
    rel="noopener noreferrer"
    :class="cls()"
    :title="label"
    :aria-label="label"
  >
    <component :is="icon" :size="16" :stroke-width="2" aria-hidden="true" />
  </a>
  <button
    v-else
    type="button"
    :class="cls()"
    :title="label"
    :aria-label="label"
    :disabled="disabled || busy"
    :aria-busy="busy || undefined"
    :aria-pressed="pressed"
    :aria-haspopup="expanded === undefined ? undefined : 'menu'"
    :aria-expanded="expanded"
    @click="$emit('click', $event)"
  >
    <component :is="icon" :size="16" :stroke-width="2" aria-hidden="true" />
  </button>
</template>
