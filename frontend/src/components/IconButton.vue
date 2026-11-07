<script setup lang="ts">
import { computed } from 'vue'

// Text button (the name is historical; actions are text, there are no icons). Renders a <RouterLink> when `to` is set, an <a> when `href` is set, else a <button>.
//  - `text` set: a page-top / toolbar button (outlined, .btn-secondary .btn-sm; `danger` -> .btn-danger).
//  - no `text`: a quiet row action (.row-act text link). Its visible text is `label`, shortened when long; pass
//    `verb` to choose the word when the label names the row ("Suspend ghost-blog" -> verb="Suspend").
// `label` is always the aria-label and the tooltip. `busy` disables it and dims it.
const props = defineProps<{
  label: string
  to?: string
  href?: string
  danger?: boolean
  sm?: boolean
  disabled?: boolean
  expanded?: boolean
  pressed?: boolean
  busy?: boolean
  text?: string
  verb?: string // the visible word of a row action, when the label names the row ("Suspend ghost" -> "Suspend")
  touch?: boolean
}>()
defineEmits<{ click: [e: MouseEvent] }>()

const shown = computed(() => {
  if (props.text) return props.text
  if (props.verb) return props.verb
  const plain = props.label.replace(/\s*\(.*\)\s*$/, '').replace(/\s+in a new tab$/, '').replace(/…$/, '').trim()
  return plain.length > 22 ? plain.split(' ')[0] : plain
})
const cls = computed(() =>
  props.text
    ? [props.danger ? 'btn-danger' : 'btn-secondary', props.sm ? 'btn-sm' : '']
    : ['row-act', props.danger ? 'row-act-danger' : ''],
)
</script>

<template>
  <RouterLink v-if="to && !disabled" :to="to" :class="cls" :title="label" :aria-label="label">{{ shown }}</RouterLink>
  <a v-else-if="href && !disabled" :href="href" target="_blank" rel="noopener noreferrer" :class="cls" :title="label" :aria-label="label">{{ shown }}</a>
  <button
    v-else
    type="button"
    :class="cls"
    :title="label"
    :aria-label="label"
    :disabled="disabled || busy"
    :aria-busy="busy || undefined"
    :aria-pressed="pressed"
    :aria-haspopup="expanded === undefined ? undefined : 'menu'"
    :aria-expanded="expanded"
    @click="$emit('click', $event)"
  >{{ shown }}</button>
</template>
