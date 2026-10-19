<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import NavBar from '../components/NavBar.vue'
import { createDbServer, listDbServers, listUsers, patchDbServer, setAppLimit } from '../api/admin'
import { ApiError } from '../api/client'
import type { AdminUser, DbServer } from '../types'

const users = ref<AdminUser[]>([])
const servers = ref<DbServer[]>([])
const limits = reactive<Record<number, number>>({})
const error = ref('')
const saving = ref(false)

const emptyForm = () => ({
  name: '',
  host: '',
  port: 3306,
  admin_user: '',
  admin_password: '',
  max_databases: 25,
})
const form = reactive(emptyForm())

const anyWarn = computed(() => servers.value.some((s) => s.warn))
const msg = (e: unknown, fallback: string) => (e instanceof ApiError ? e.message : fallback)

async function load() {
  try {
    ;[users.value, servers.value] = await Promise.all([listUsers(), listDbServers()])
    for (const u of users.value) limits[u.id] = u.app_limit
  } catch (e) {
    error.value = msg(e, 'Failed to load admin data')
  }
}
onMounted(load)

async function saveLimit(u: AdminUser) {
  error.value = ''
  try {
    const updated = await setAppLimit(u.id, limits[u.id])
    users.value = users.value.map((x) => (x.id === u.id ? { ...x, ...updated } : x))
  } catch (e) {
    error.value = msg(e, 'Failed to update app limit')
  }
}

async function patch(s: DbServer, payload: { status?: string; max_databases?: number }) {
  error.value = ''
  try {
    const updated = await patchDbServer(s.id, payload)
    servers.value = servers.value.map((x) => (x.id === s.id ? updated : x))
  } catch (e) {
    error.value = msg(e, 'Failed to update DB server')
    await load()
  }
}

async function addServer() {
  error.value = ''
  saving.value = true
  try {
    await createDbServer({ ...form })
    Object.assign(form, emptyForm()) // clears the password
    servers.value = await listDbServers()
  } catch (e) {
    error.value = msg(e, 'Failed to add DB server')
  } finally {
    saving.value = false
  }
}

const input =
  'rounded-md border border-pico-border bg-pico-card px-2 py-1 text-sm text-white focus:outline-none focus:border-accent'
</script>

<template>
  <div class="min-h-screen bg-pico-bg">
    <NavBar />
    <main class="mx-auto max-w-4xl space-y-6 px-4 py-6">
      <h1 class="text-lg font-semibold text-white">Admin</h1>

      <div v-if="anyWarn" class="rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-sm text-amber-400">
        DB capacity above 80%, file a capacity request.
      </div>
      <div v-if="error" class="rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">
        {{ error }}
      </div>

      <section>
        <h2 class="mb-2 text-sm font-semibold text-zinc-300">Users</h2>
        <table class="w-full rounded-md border border-pico-border bg-pico-panel text-left text-sm text-zinc-300">
          <thead class="text-xs text-zinc-500">
            <tr><th class="p-2">Email</th><th class="p-2">Role</th><th class="p-2">Apps</th><th class="p-2">App limit</th></tr>
          </thead>
          <tbody>
            <tr v-for="u in users" :key="u.id" class="border-t border-pico-border">
              <td class="p-2">{{ u.email }}</td>
              <td class="p-2">{{ u.role }}</td>
              <td class="p-2">{{ u.app_count }}</td>
              <td class="p-2">
                <form class="flex items-center gap-2" @submit.prevent="saveLimit(u)">
                  <input v-model.number="limits[u.id]" type="number" min="0" :class="[input, 'w-20']" />
                  <button
                    v-if="limits[u.id] !== u.app_limit"
                    type="submit"
                    class="rounded bg-accent-dark px-2 py-1 text-xs text-white hover:bg-accent"
                  >
                    Save
                  </button>
                </form>
              </td>
            </tr>
          </tbody>
        </table>
      </section>

      <section>
        <h2 class="mb-2 text-sm font-semibold text-zinc-300">DB servers</h2>
        <table class="w-full rounded-md border border-pico-border bg-pico-panel text-left text-sm text-zinc-300">
          <thead class="text-xs text-zinc-500">
            <tr><th class="p-2">Name</th><th class="p-2">Host</th><th class="p-2">Capacity</th><th class="p-2">Status</th></tr>
          </thead>
          <tbody>
            <tr v-for="s in servers" :key="s.id" class="border-t border-pico-border">
              <td class="p-2">{{ s.name }}</td>
              <td class="p-2 font-mono text-xs">{{ s.host }}:{{ s.port }}</td>
              <td class="p-2">
                <div class="h-2 w-40 overflow-hidden rounded bg-pico-card">
                  <div
                    :class="['h-full', s.warn ? 'bg-amber-500' : 'bg-accent']"
                    :style="{ width: Math.min(100, s.max_databases ? (s.used / s.max_databases) * 100 : 100) + '%' }"
                  />
                </div>
                <span class="text-xs" :class="s.warn ? 'text-amber-400' : 'text-zinc-500'">
                  {{ s.used }}/{{ s.max_databases }}
                </span>
              </td>
              <td class="p-2">
                <select
                  :value="s.status"
                  :class="input"
                  @change="patch(s, { status: ($event.target as HTMLSelectElement).value })"
                >
                  <option>active</option>
                  <option>draining</option>
                  <option>full</option>
                </select>
              </td>
            </tr>
            <tr v-if="servers.length === 0"><td colspan="4" class="p-3 text-zinc-500">No DB servers.</td></tr>
          </tbody>
        </table>

        <form class="mt-3 grid grid-cols-2 gap-2 rounded-md border border-pico-border bg-pico-panel p-3 sm:grid-cols-3" @submit.prevent="addServer">
          <input v-model="form.name" required placeholder="name" :class="input" />
          <input v-model="form.host" required placeholder="host" :class="input" />
          <input v-model.number="form.port" required type="number" min="1" max="65535" placeholder="port" :class="input" />
          <input v-model="form.admin_user" required placeholder="admin user" autocomplete="off" :class="input" />
          <input
            v-model="form.admin_password"
            required
            type="password"
            placeholder="admin password"
            autocomplete="new-password"
            :class="input"
          />
          <input v-model.number="form.max_databases" required type="number" min="1" placeholder="max databases" :class="input" />
          <div class="col-span-full flex justify-end">
            <button
              type="submit"
              :disabled="saving"
              class="rounded-md bg-accent-dark px-3 py-1.5 text-sm font-semibold text-white hover:bg-accent disabled:opacity-50"
            >
              {{ saving ? 'Adding…' : 'Add DB server' }}
            </button>
          </div>
        </form>
      </section>
    </main>
  </div>
</template>
