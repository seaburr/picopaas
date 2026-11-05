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
  text?: string // visible label from sm up; icon-only below sm. Rule: set it on every action at the top of a page; repeated row actions stay icon-only
  touch?: boolean // 44px hit area below sm
}>()
defineEmits<{ click: [e: MouseEvent] }>()
const cls = () => ['btn-icon', props.busy ? 'btn-icon-busy' : '', props.sm ? 'btn-icon-sm' : '', props.touch ? 'btn-icon-touch' : '', props.text ? 'btn-icon-text' : '', props.danger ? 'btn-icon-danger' : '']
</script>

<template>
  <RouterLink v-if="to && !disabled" :to="to" :class="cls()" :title="label" :aria-label="label">
    <component :is="icon" :size="16" :stroke-width="2" aria-hidden="true" /><span v-if="text" class="hidden sm:inline" aria-hidden="true">{{ text }}</span>
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
    <component :is="icon" :size="16" :stroke-width="2" aria-hidden="true" /><span v-if="text" class="hidden sm:inline" aria-hidden="true">{{ text }}</span>
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
    <component :is="icon" :size="16" :stroke-width="2" aria-hidden="true" /><span v-if="text" class="hidden sm:inline" aria-hidden="true">{{ text }}</span>
  </button>
</template>
