<script setup lang="ts">
import { computed, onMounted, ref, useId, watch } from 'vue'
import { acknowledgeSecurityAlert, listSecurityAlerts, securityAlertsSummary } from '../api/admin'
import { ApiError } from '../api/client'
import { ALERT_SEVERITY } from '../composables/advice'
import { ago, stamp, stampTitle } from '../utils/format'
import type { AdminUser, SecurityAlert } from '../types'

// Admin > Security alerts: rule-based alerts from the audit log, login throttle and Kaisen spend.
const props = defineProps<{ users: AdminUser[] }>()
const emit = defineEmits<{ open: [n: number] }>()
const uid = useId()
const rows = ref<SecurityAlert[]>([])
const loading = ref(true)
const error = ref('')
const showAll = ref(false)
const acking = ref<number | null>(null)

async function load() {
  loading.value = true
  try {
    rows.value = await listSecurityAlerts(showAll.value ? 'all' : 'open')
    error.value = ''
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : "Couldn't load security alerts."
  } finally {
    loading.value = false
  }
}
onMounted(load)
watch(showAll, load)

async function ack(a: SecurityAlert) {
  acking.value = a.id
  error.value = ''
  try {
    const done = await acknowledgeSecurityAlert(a.id)
    rows.value = showAll.value ? rows.value.map((r) => (r.id === a.id ? done : r)) : rows.value.filter((r) => r.id !== a.id)
    securityAlertsSummary().then((r) => emit('open', r.open)).catch(() => {})
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : "Couldn't acknowledge the alert."
  } finally {
    acking.value = null
  }
}

const emailOf = computed(() => new Map(props.users.map((u) => [String(u.id), u.email])))
const evidence = (a: SecurityAlert) => Object.entries(a.evidence ?? {}).map(([k, v]) => `${k}: ${typeof v === 'object' ? JSON.stringify(v) : v}`).join(', ')
</script>

<template>
  <div class="space-y-3">
    <p class="help">
      Machine-speed identity activity, spend spikes and login pressure, checked every few minutes. Acknowledge an alert once
      you have handled it; the same condition merges into one open alert, and new evidence after an acknowledge opens a new one.
    </p>
    <label :for="`${uid}-all`" class="flex items-center gap-2 text-sm text-zinc-300">
      <input :id="`${uid}-all`" v-model="showAll" type="checkbox" class="check" /> Show acknowledged
    </label>
    <p v-if="error" role="alert" class="alert-error">{{ error }}</p>
    <p v-if="loading && rows.length === 0" class="meta" role="status" aria-busy="true">Loading security alerts…</p>
    <p v-else-if="!error && rows.length === 0" class="sec meta py-2">{{ showAll ? 'No security alerts.' : 'No open security alerts.' }}</p>
    <div v-else-if="rows.length" class="overflow-x-auto">
      <table class="tbl tbl-stack md:min-w-[60rem]" aria-label="Security alerts">
        <colgroup><col class="w-24" /><col /><col class="w-64" /><col class="w-16" /><col class="w-28" /><col class="w-28" /><col class="w-32" /></colgroup>
        <thead>
          <tr><th scope="col">Severity</th><th scope="col">What happened</th><th scope="col">Subject</th><th scope="col">Count</th><th scope="col">First seen</th><th scope="col">Last seen</th><th scope="col"><span class="sr-only">Actions</span></th></tr>
        </thead>
        <tbody class="text-zinc-200">
          <tr v-for="a in rows" :key="a.id" :title="a.kind" class="align-top">
            <td><span :class="['st', `tone-${ALERT_SEVERITY[a.severity].tone}`]"><span :class="['dot', `dot-${ALERT_SEVERITY[a.severity].tone}`]" aria-hidden="true" />{{ ALERT_SEVERITY[a.severity].word }}</span></td>
            <td class="wrap-break-word">
              {{ a.message }}
              <div v-if="evidence(a)" class="meta mono wrap-break-word">{{ evidence(a) }}</div>
            </td>
            <td class="wrap-break-word">
              <template v-if="a.subject_type && a.subject_id">
                <RouterLink v-if="a.subject_type === 'user' && emailOf.get(a.subject_id)" :to="{ path: '/admin', query: { q: emailOf.get(a.subject_id) } }" class="link break-all">{{ emailOf.get(a.subject_id) }}</RouterLink>
                <span v-else class="meta">{{ a.subject_type.replace('_', ' ') }} <span class="mono">{{ a.subject_id }}</span></span>
              </template>
              <span v-else class="meta">None</span>
            </td>
            <td class="mono">{{ a.count }}</td>
            <td class="meta whitespace-nowrap" :title="stampTitle(a.first_seen)">{{ ago(a.first_seen) || stamp(a.first_seen) }}</td>
            <td class="meta whitespace-nowrap" :title="stampTitle(a.last_seen)">{{ ago(a.last_seen) || stamp(a.last_seen) }}</td>
            <td>
              <div class="row-acts">
                <button v-if="!a.acknowledged_at" type="button" class="row-act" :disabled="acking === a.id" :aria-busy="acking === a.id" @click="ack(a)">Acknowledge</button>
                <span v-else class="meta" :title="stampTitle(a.acknowledged_at)">Acknowledged</span>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
