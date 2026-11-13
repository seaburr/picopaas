<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import ConfirmDialog from './ConfirmDialog.vue'
import { SEVERITY, resourceField } from '../composables/advice'
import { getAppAdvice, updateApp } from '../api/apps'
import { ApiError } from '../api/client'
import { useAppsStore } from '../stores/apps'
import { useToastStore } from '../stores/toast'
import type { AdviceAction, AdviceFinding } from '../types'

// Health advisor findings for one app. Renders nothing while loading, when empty and on any load error:
// advice is secondary and must never break or shift the health page.
const props = defineProps<{ appId: number; disabled?: boolean }>()
const emit = defineEmits<{ applied: [] }>()
const store = useAppsStore()
const toast = useToastStore()

const findings = ref<AdviceFinding[]>([])
let seq = 0
async function load() {
  const mine = ++seq
  try {
    const r = await getAppAdvice(props.appId)
    if (mine === seq) findings.value = r.findings ?? []
  } catch {
    if (mine === seq) findings.value = []
  }
}
watch(() => props.appId, load, { immediate: true })
defineExpose({ load })

type Pending = Extract<AdviceAction, { kind: 'set_resource' | 'rollback' }>
const target = ref<Pending | null>(null)
const busy = ref(false)
const err = ref('')

// Only actions this UI can send are offered a button; an unknown resource field gets none.
const canApply = (a: AdviceAction | null): a is Pending => !!a && (a.kind === 'rollback' || (a.kind === 'set_resource' && !!resourceField(a.field)))
const effect = (a: Pending) => (a.restarts_app ? ' Restarts the app (about 10 s of downtime with 1 replica).' : ' Applies without a restart.')
const confirmText = computed(() => {
  const a = target.value
  if (!a) return { title: '', message: '', label: 'Apply' }
  if (a.kind === 'rollback') return { title: 'Roll back the image?', message: `${a.label}.${effect(a)}`, label: 'Apply' }
  const f = resourceField(a.field)!
  return { title: `Change ${f.label.toLowerCase()}?`, message: `${f.label} ${a.from} ${a.unit} → ${a.to} ${a.unit}.${effect(a)}`, label: 'Apply' }
})

async function confirm() {
  const a = target.value
  if (!a) return
  busy.value = true
  err.value = ''
  try {
    if (a.kind === 'set_resource') {
      const f = resourceField(a.field)!
      // The existing owner resource change (same call as the Settings form): only this field changes.
      store.upsert(await updateApp(props.appId, { resources: { [f.key]: a.to } }))
    } else {
      await store.rollback(props.appId, a.digest) // the image the finding names, not "whatever came before"
    }
    toast.success(a.kind === 'rollback' ? 'Rolling back. This takes about a minute.' : 'Change applied.')
    target.value = null
    emit('applied')
    await load()
  } catch (e) {
    target.value = null
    err.value = `Couldn't apply: ${e instanceof ApiError ? e.message : 'try again'}`
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <section v-if="findings.length" class="sec sec-pad" aria-labelledby="h-advice">
    <h2 id="h-advice" class="section-title mb-2 pt-2">Advice</h2>
    <p v-if="err" role="alert" class="alert-error mb-2">{{ err }}</p>
    <table class="tbl max-lg:block max-lg:[&_tbody]:block max-lg:[&_tr]:block max-lg:[&_td]:block max-lg:[&_td]:border-0" aria-label="Advice for this app">
      <colgroup class="max-lg:hidden"><col class="w-24" /><col class="w-56" /><col /><col class="w-64" /></colgroup>
      <thead class="max-lg:hidden">
        <tr><th scope="col">Severity</th><th scope="col">Finding</th><th scope="col">Evidence</th><th scope="col"><span class="sr-only">Action</span></th></tr>
      </thead>
      <tbody>
        <tr v-for="f in findings" :key="f.rule" :title="f.rule" class="align-top max-lg:border-b max-lg:border-pico-border max-lg:py-2">
          <td><span :class="['st', `tone-${SEVERITY[f.severity].tone}`]"><span :class="['dot', `dot-${SEVERITY[f.severity].tone}`]" aria-hidden="true" />{{ SEVERITY[f.severity].word }}</span></td>
          <td class="wrap-break-word font-medium text-zinc-100">{{ f.title }}</td>
          <td class="wrap-break-word text-zinc-300">{{ f.evidence }}</td>
          <td>
            <div class="row-acts">
              <RouterLink v-if="f.action?.kind === 'link'" :to="f.action.to" class="row-act">{{ f.action.label }}</RouterLink>
              <button v-else-if="canApply(f.action)" type="button" class="row-act" :disabled="disabled" @click="target = f.action">{{ f.action.label }}</button>
            </div>
          </td>
        </tr>
      </tbody>
    </table>
    <ConfirmDialog
      :open="target !== null"
      :title="confirmText.title"
      :message="confirmText.message"
      :confirm-label="confirmText.label"
      busy-label="Applying…"
      :busy="busy"
      @confirm="confirm"
      @cancel="target = null"
    />
  </section>
</template>
