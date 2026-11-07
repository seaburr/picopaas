<script setup lang="ts">
import { ref } from 'vue'
import PasswordField from './PasswordField.vue'
import { ApiError } from '../api/client'
import { deleteBuildSecret, putBuildSecret } from '../api/kaisen'
import { useToastStore } from '../stores/toast'
import type { KaisenBuildSecret } from '../types'

const props = defineProps<{ buildId: number; secrets: KaisenBuildSecret[]; canEdit: boolean; canDeploy: boolean; deploying: boolean }>()
const emit = defineEmits<{ changed: [secrets: KaisenBuildSecret[]]; deploy: [] }>()
const toast = useToastStore()

const name = ref('')
const value = ref('')
const busy = ref(false)
const removing = ref('')
const error = ref('')
const pendingDeploy = ref(false)

async function add() {
  error.value = ''
  const n = name.value.trim()
  if (!n || !value.value) {
    error.value = 'Enter a name and a value.'
    return
  }
  busy.value = true
  try {
    const r = await putBuildSecret(props.buildId, n, value.value)
    emit('changed', r.secrets)
    pendingDeploy.value = r.applies_on_next_deploy
    name.value = ''
    value.value = ''
    toast.success(`Saved ${n}.`)
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : "Couldn't save the secret."
  } finally {
    busy.value = false
  }
}
async function remove(n: string) {
  error.value = ''
  removing.value = n
  try {
    const r = await deleteBuildSecret(props.buildId, n)
    emit('changed', r.secrets)
    pendingDeploy.value = true
    toast.success(`Removed ${n}.`)
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : "Couldn't remove the secret."
  } finally {
    removing.value = ''
  }
}
</script>

<template>
  <details class="disc sec py-1.5" aria-labelledby="h-secrets">
    <summary><h2 id="h-secrets" class="section-title">Secrets <span class="mono font-normal text-zinc-400">{{ secrets.length }}</span></h2></summary>
    <div class="space-y-3 pb-2 pt-1">
    <p class="help">Environment values for the built app, such as an API key. Kaisen sees only the names, never the values. Values apply on the next deploy. Saving a name that exists replaces its value.</p>

    <p v-if="secrets.length === 0" class="meta">No secrets yet.</p>
    <ul v-else class="border-t border-pico-border">
      <li v-for="s in secrets" :key="s.name" class="flex items-center justify-between gap-2 border-b border-pico-border py-1">
        <span class="mono break-all text-sm text-zinc-200">{{ s.name }}</span>
        <span class="row-acts shrink-0 gap-x-2.5">
          <span class="mono meta">{{ s.value }}</span>
          <button v-if="canEdit" type="button" class="row-act row-act-danger" :aria-label="`Delete ${s.name}`" :disabled="removing === s.name" :aria-busy="removing === s.name || undefined" @click="remove(s.name)">Delete</button>
        </span>
      </li>
    </ul>

    <form v-if="canEdit" class="space-y-3" novalidate @submit.prevent="add">
      <div>
        <label for="bs-name" class="label">Name</label>
        <input id="bs-name" v-model="name" class="input mono" autocomplete="off" autocapitalize="characters" spellcheck="false" placeholder="STRIPE_API_KEY" :aria-invalid="!!error" />
      </div>
      <PasswordField v-model="value" label="Value" autocomplete="off" :required="false" />
      <div>
        <button type="submit" class="btn-secondary" :disabled="busy">{{ busy ? 'Saving…' : 'Add or replace secret' }}</button>
      </div>
    </form>
    <p v-if="error" class="field-error whitespace-pre-wrap" role="alert">{{ error }}</p>

    <div v-if="pendingDeploy" class="alert-info flex flex-wrap items-center justify-between gap-2" role="status">
      <span>Changes apply on the next deploy.</span>
      <button v-if="canDeploy" type="button" class="btn-secondary btn-sm" :disabled="deploying" @click="emit('deploy'); pendingDeploy = false">{{ deploying ? 'Deploying…' : 'Deploy now to apply' }}</button>
    </div>
    </div>
  </details>
</template>
