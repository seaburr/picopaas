<script setup lang="ts">
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import BackupSettings from '../components/BackupSettings.vue'
import AppSettingsPanel from '../components/AppSettingsPanel.vue'
import EnvVarEditor from '../components/EnvVarEditor.vue'
import HostLink from '../components/HostLink.vue'
import NavBar from '../components/NavBar.vue'
import SpecEditor from '../components/SpecEditor.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { ApiError } from '../api/client'
import { listTemplates } from '../api/templates'
import { SAMPLE_APP, primaryHost } from '../composables/appUi'
import { envKeyError } from '../composables/envKeys'
import { useAppsStore } from '../stores/apps'
import { useAuthStore } from '../stores/auth'
import { useToastStore } from '../stores/toast'
import type { App, EnvVar } from '../types'

const props = defineProps<{ id?: string }>()
const router = useRouter()
const route = useRoute()
const store = useAppsStore()
const auth = useAuthStore()
const toast = useToastStore()

const isEdit = computed(() => props.id !== undefined)
const appId = computed(() => (props.id !== undefined ? Number(props.id) : null))
const loadedApp = ref<App | null>(null)

const form = reactive({
  name: '',
  image: '',
  port: 8080 as number | string,
  want_db: false,
  db_engine: 'mysql' as 'mysql' | 'pg',
  want_storage: false,
  health_check_enabled: false,
  health_check_path: '/healthz',
  health_check_timeout: '' as number | string, // blank = platform default
  auto_update: true,
})
const isDigestRef = computed(() => form.image.includes('@'))
const env = ref<EnvVar[]>([])
const originalImage = ref('')

const tab = ref<'form' | 'yaml'>('form')
const suspended = ref(false)
// Also true while a template loads: EnvVarEditor copies its rows once on mount, so it must mount after the prefill.
const loading = ref(isEdit.value || typeof route.query.template === 'string')
const saving = ref(false)
const error = ref('')
const fieldErrors = ref<Record<string, string>>({})
const errorBox = ref<HTMLElement | null>(null)

const limits = computed(() => auth.user?.limits)
const dbOff = computed(() => auth.user?.features?.databases === false)
const storageOff = computed(() => auth.user?.features?.storage === false)
const dbLabel = computed(() => `${limits.value?.db_mb ?? 50} MB`)

// Which engines a new database can use. When the backend doesn't report db_engines (older build), fall back to MySQL.
const availableEngines = computed(() => auth.user?.features?.db_engines ?? ['mysql'])
const engineChoices = computed(() =>
  (
    [
      { value: 'mysql', label: 'MySQL' },
      { value: 'pg', label: 'PostgreSQL' },
    ] as const
  ).map((e) => ({ ...e, available: availableEngines.value.includes(e.value) })),
)

// Ticking Database should land on an engine that actually has capacity (and the engine is immutable, so only on create).
watch(
  () => form.want_db,
  (on) => {
    if (on && !isEdit.value && !availableEngines.value.includes(form.db_engine)) {
      form.db_engine = (availableEngines.value[0] as 'mysql' | 'pg') ?? 'mysql'
    }
  },
)
const volumesOn = computed(() => auth.user?.features?.volumes === true)
const volMax = computed(() => limits.value?.volume_max_gi ?? 1)
const vol = reactive({ enabled: false, size: 1 as number | string, mount: '/data' })
const volAttached = ref(false) // app already has a volume (can't be detached)
const originalMount = ref('')
const originalSize = ref(0)
vol.size = limits.value?.volume_default_gi ?? 1
vol.mount = limits.value?.volume_default_mount_path ?? '/data'
// Admin-granted sizes above the user max may be re-sent unchanged.
const volSizeError = computed(() => {
  if (fieldErrors.value['volume.size_gi']) return fieldErrors.value['volume.size_gi']
  if (!vol.enabled) return ''
  const n = Number(vol.size)
  if (!Number.isInteger(n) || n < 1) return 'Use a whole number of GiB, at least 1.'
  if (volAttached.value && n === originalSize.value) return ''
  return n > volMax.value ? `At most ${volMax.value} GiB.` : ''
})
const volMountError = computed(() => {
  if (fieldErrors.value['volume.mount_path']) return fieldErrors.value['volume.mount_path']
  if (!vol.enabled) return ''
  return vol.mount.startsWith('/') && !/\s/.test(vol.mount) ? '' : 'Mount path must be absolute (start with /) and have no spaces.'
})
function volumePayload(): { size_gi?: number; mount_path?: string } | undefined {
  if (!vol.enabled) return undefined
  if (!volAttached.value) return { size_gi: Number(vol.size), mount_path: vol.mount }
  const out: { size_gi?: number; mount_path?: string } = {}
  if (Number(vol.size) !== originalSize.value) out.size_gi = Number(vol.size)
  if (vol.mount !== originalMount.value) out.mount_path = vol.mount
  return Object.keys(out).length ? out : undefined
}
const bucketLabel = computed(() => `${limits.value?.bucket_mb ?? 100} MB`)
const imageMb = computed(() => limits.value?.image_mb ?? 512)

const imageError = computed(() => {
  if (fieldErrors.value.image) return fieldErrors.value.image
  return error.value.toLowerCase().startsWith('image') ? error.value : ''
})
const formError = computed(() => (error.value && !error.value.toLowerCase().startsWith('image') ? error.value : ''))

const templateNote = ref<{ name: string; notes: string } | null>(null)
const templateSecrets = ref<{ key: string; hint: string }[]>([])
const templateHints = ref<string[]>([])

onMounted(async () => {
  if (isEdit.value && appId.value !== null) {
    try {
      const app = await store.fetchOne(appId.value)
      loadedApp.value = app
      suspended.value = app.status === 'suspended'
      form.name = app.name
      form.image = app.image
      originalImage.value = app.image
      form.port = app.port
      form.want_db = app.want_db
      form.db_engine = app.db_engine ?? 'mysql'
      form.want_storage = app.want_storage
      form.health_check_enabled = app.health_check_enabled ?? false
      form.health_check_path = app.health_check_path || '/healthz'
      form.health_check_timeout = app.health_check_timeout_seconds ?? ''
      form.auto_update = app.auto_update ?? true
      env.value = app.env
      if (app.volume) {
        vol.enabled = true
        volAttached.value = true
        vol.size = app.volume.size_gi
        vol.mount = app.volume.mount_path
        originalSize.value = app.volume.size_gi
        originalMount.value = app.volume.mount_path
      }
      document.title = `Settings · ${app.name} · PicoPaaS`
    } catch (e) {
      error.value = e instanceof ApiError ? e.message : "Couldn't load this app."
    } finally {
      loading.value = false
    }
  } else if (route.query.sample) {
    form.name = SAMPLE_APP.name
    form.image = SAMPLE_APP.image
    form.port = SAMPLE_APP.port
  } else if (typeof route.query.template === 'string') {
    try {
      const t = (await listTemplates()).find((x) => x.id === route.query.template)
      if (t) {
        form.name = t.id
        form.image = t.image
        form.port = t.port
        form.want_db = t.wants_db && !dbOff.value
        form.db_engine = t.db_engine ?? 'mysql'
        form.want_storage = t.wants_storage && !storageOff.value
        form.health_check_enabled = !!t.health_check
        if (t.health_check) {
          form.health_check_path = t.health_check.path
          form.health_check_timeout = t.health_check.timeout_seconds ?? ''
        }
        if (t.volume && volumesOn.value) {
          vol.enabled = true
          vol.size = Math.min(t.volume.size_gi, volMax.value)
          vol.mount = t.volume.mount_path
        }
        env.value = [
          ...Object.entries(t.env).map(([key, value]) => ({ key, value, is_secret: false })),
          ...(t.secrets ?? []).map((s) => ({ key: s.key, value: '', is_secret: true })),
        ]
        const hints: string[] = []
        if (t.volume && !volumesOn.value) hints.push('This instance has persistent volumes turned off, so the volume was skipped.')
        if (t.wants_db && dbOff.value) hints.push('Databases are turned off here, so the database was skipped.')
        if (t.wants_storage && storageOff.value) hints.push('Blob storage is turned off here, so the bucket was skipped.')
        if (t.wants_db && t.db_engine === 'pg' && !dbOff.value && !availableEngines.value.includes('pg')) hints.push('PostgreSQL has no free capacity right now.')
        templateSecrets.value = t.secrets ?? []
        templateHints.value = hints
        templateNote.value = { name: t.name, notes: t.notes ?? '' }
      } else {
        error.value = `Unknown template "${route.query.template}".`
      }
    } catch {
      error.value = "Couldn't load the template."
    } finally {
      loading.value = false
    }
  }
})

const timeoutValue = computed<number | null>(() => (String(form.health_check_timeout).trim() === '' ? null : Number(form.health_check_timeout)))
const timeoutError = computed(() => {
  if (fieldErrors.value.health_check_timeout_seconds) return fieldErrors.value.health_check_timeout_seconds
  if (!form.health_check_enabled || timeoutValue.value === null) return ''
  const t = timeoutValue.value
  return Number.isInteger(t) && t >= 1 && t <= 90 ? '' : 'Timeout must be a whole number from 1 to 90.'
})

const pathError = computed(() => {
  if (fieldErrors.value.health_check_path) return fieldErrors.value.health_check_path
  if (!form.health_check_enabled) return ''
  const p = form.health_check_path
  if (!p.startsWith('/')) return 'Path must start with /'
  if (/\s/.test(p)) return 'Path must not contain spaces'
  return ''
})

// Empty rows are dropped before sending; keep the mapping so server errors (env.N.key) land on the right editor row.
const keptIdx = computed(() => env.value.map((e, i) => (e.key || e.value ? i : -1)).filter((i) => i >= 0))
const envErrors = computed(() => {
  const out: Record<string, string> = {}
  for (const [path, msg] of Object.entries(fieldErrors.value)) {
    const m = path.match(/^env\.(\d+)\.(key|value)$/)
    if (m) out[`${keptIdx.value[Number(m[1])] ?? m[1]}.${m[2]}`] = msg
  }
  return out
})
const envInvalid = computed(() => env.value.some((e) => (e.key || e.value) && envKeyError(e.key)))

function onField(name: string) {
  if (fieldErrors.value[name]) fieldErrors.value = { ...fieldErrors.value, [name]: '' }
  if (name === 'image') error.value = error.value.toLowerCase().startsWith('image') ? '' : error.value
}

async function showError() {
  await nextTick()
  errorBox.value?.scrollIntoView({ block: 'center', behavior: 'smooth' })
}

// Only loadedApp changes, so unsaved form fields are untouched.
function onPanelUpdate(a: App) {
  loadedApp.value = a
  suspended.value = a.status === 'suspended'
}

const busyLabel = computed(() => (!isEdit.value || form.image !== originalImage.value ? 'Checking image…' : 'Saving…'))

// Pods run non-root with all capabilities dropped, so privileged ports (<1024) can never be bound.
function portError(): string {
  const n = Number(form.port)
  if (form.port === '' || !Number.isInteger(n) || n < 1024 || n > 65535)
    return "Use a port from 1024 to 65535."
  return ''
}

async function onSubmit() {
  error.value = ''
  fieldErrors.value = {}
  const portErr = portError()
  if (portErr) {
    fieldErrors.value = { port: portErr }
    error.value = 'Some fields need attention.'
    return showError()
  }
  if (pathError.value || timeoutError.value) return
  if (volSizeError.value || volMountError.value) {
    error.value = 'Fix the persistent volume settings.'
    return showError()
  }
  if (envInvalid.value) {
    error.value = 'Fix the highlighted environment variable names.'
    return showError()
  }
  const envOut = keptIdx.value.map((i) => env.value[i])
  const port = Number(form.port)
  saving.value = true
  try {
    if (isEdit.value && appId.value !== null) {
      const app = await store.update(appId.value, {
        name: form.name.trim(),
        image: form.image.trim(),
        port,
        health_check_enabled: form.health_check_enabled,
        health_check_path: form.health_check_path,
        health_check_timeout_seconds: form.health_check_enabled ? timeoutValue.value : null,
        auto_update: form.auto_update,
        ...(volumePayload() ? { volume: volumePayload() } : {}),
        env: envOut,
      })
      toast.success(`Saved. Redeploying ${app.name}…`)
      await router.push(`/apps/${app.id}`)
    } else {
      const app = await store.create({
        name: form.name.trim(),
        image: form.image.trim(),
        port,
        want_db: form.want_db,
        db_engine: form.db_engine,
        want_storage: form.want_storage,
        health_check_enabled: form.health_check_enabled,
        health_check_path: form.health_check_path,
        health_check_timeout_seconds: form.health_check_enabled ? timeoutValue.value : null,
        auto_update: form.auto_update,
        ...(volumePayload() ? { volume: volumePayload() } : {}),
        env: envOut,
      })
      toast.success(`${app.name} created. Deploying, usually about a minute.`)
      await router.push(`/apps/${app.id}`)
    }
  } catch (e) {
    if (e instanceof ApiError) {
      fieldErrors.value = e.fieldErrors
      error.value = Object.keys(e.fieldErrors).length && e.status === 422 ? 'Some fields need attention.' : e.message
      // Backend image checks (not found / private / root / too big) are plain 422 strings that mention "image".
      if (e.status === 422 && /\bimage\b/i.test(e.message) && !Object.keys(e.fieldErrors).length) {
        fieldErrors.value = { image: e.message }
      }
      if (fieldErrors.value.image) error.value = ''
    } else {
      error.value = "Couldn't save the app."
    }
    await showError()
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <div class="min-h-screen bg-pico-bg">
    <NavBar />

    <main id="main" tabindex="-1" :class="['mx-auto px-4 py-4', isEdit ? 'max-w-6xl sm:px-6' : 'max-w-2xl']">
      <nav v-if="isEdit" class="meta mb-2" aria-label="Breadcrumb">
        <RouterLink to="/apps" class="link">Apps</RouterLink>
        <span aria-hidden="true"> / </span>
        <RouterLink v-if="loadedApp" :to="`/apps/${loadedApp.id}`" class="link">{{ loadedApp.name }}</RouterLink>
        <span aria-hidden="true"> / </span>Settings
      </nav>
      <nav v-else class="meta mb-2" aria-label="Breadcrumb">
        <RouterLink to="/apps" class="link">Apps</RouterLink><span aria-hidden="true"> / </span>New app
      </nav>

      <div class="mb-3 flex flex-wrap items-start justify-between gap-3">
        <div class="min-w-0">
          <div class="flex flex-wrap items-center gap-2">
            <h1 class="page-title break-all">{{ isEdit ? (loadedApp?.name ?? 'Settings') : 'New app' }}</h1>
            <StatusBadge v-if="loadedApp" :status="loadedApp.status" />
          </div>
          <HostLink v-if="loadedApp" :hostname="primaryHost(loadedApp)" class="mt-1" />
          <p v-if="!isEdit" class="help">
            Deploy a public Docker image. You'll get an https URL as soon as the app is created.
            <RouterLink to="/templates" class="link">Browse templates</RouterLink>
          </p>
        </div>
        <RouterLink v-if="isEdit && loadedApp" :to="`/apps/${loadedApp.id}`" class="btn-secondary">App health</RouterLink>
      </div>

      <p v-if="templateNote" class="alert-info mb-3">
        <strong class="font-medium">Prefilled from template: {{ templateNote.name }}.</strong> {{ templateNote.notes }}
      </p>
      <p v-for="h in templateHints" :key="h" class="alert-warn mb-3">{{ h }}</p>
      <div v-if="templateSecrets.length" class="alert-info mb-3 text-sm">
        <strong class="font-medium">Set these secrets in Environment variables before creating:</strong>
        <ul class="mt-1 list-disc space-y-0.5 pl-5">
          <li v-for="sec in templateSecrets" :key="sec.key" class="min-w-0 break-words">
            <span class="mono">{{ sec.key }}</span>: {{ sec.hint }}
          </li>
        </ul>
      </div>

      <div v-if="!isEdit" class="mb-3 flex gap-1 border-b border-pico-border" role="tablist">
        <button
          v-for="t in (['form', 'yaml'] as const)"
          :key="t"
          type="button"
          role="tab"
          :aria-selected="tab === t"
          :class="['-mb-px border-b-2 px-3 py-1.5 text-sm', tab === t ? 'border-accent-light text-white' : 'border-transparent text-zinc-400 hover:text-white']"
          @click="tab = t"
        >
          {{ t === 'form' ? 'Form' : 'YAML (advanced)' }}
        </button>
      </div>

      <SpecEditor v-if="!isEdit && tab === 'yaml'" />

      <div v-else-if="loading" class="text-sm text-zinc-400" aria-busy="true">Loading…</div>

      <div v-else-if="isEdit && !loadedApp" role="alert" class="alert-error">
        {{ error || "Couldn't load this app." }}
        <RouterLink to="/apps" class="link ml-1">Back to apps</RouterLink>
      </div>

      <template v-else>
      <h2 v-if="isEdit" class="section-title mb-2">Deployment <span class="text-sm font-normal text-zinc-400">(managed by your YAML spec)</span></h2>
      <form class="card card-pad space-y-4" novalidate @submit.prevent="onSubmit">
        <div v-if="suspended" class="alert-warn">Suspended by an administrator. This app can't be edited. Contact the administrator to restore it.</div>
        <fieldset :disabled="suspended" :class="['space-y-4', isEdit ? 'max-w-3xl' : '']">
          <div>
            <label for="f-name" class="label">Name</label>
            <input
              id="f-name"
              v-model="form.name"
              type="text"
              required
              maxlength="255"
              placeholder="my-app"
              aria-describedby="f-name-help"
              :aria-invalid="!!fieldErrors.name || undefined"
              class="input"
              @input="onField('name')"
            />
            <p v-if="fieldErrors.name" class="field-error" role="alert">{{ fieldErrors.name }}</p>
            <p id="f-name-help" class="help">Display name only. {{ isEdit ? 'Renaming does not change the URL.' : 'Your URL is generated after you create the app.' }}</p>
          </div>

          <div>
            <label for="f-image" class="label">Image</label>
            <input
              id="f-image"
              v-model="form.image"
              type="text"
              required
              maxlength="512"
              placeholder="ghcr.io/me/app:1.2"
              spellcheck="false"
              autocapitalize="off"
              aria-describedby="f-image-help"
              :aria-invalid="!!imageError || undefined"
              class="input mono"
              @input="onField('image')"
            />
            <p v-if="imageError" class="field-error" role="alert">{{ imageError }}</p>
            <p id="f-image-help" class="help">A public image, e.g. <code class="mono">ghcr.io/me/app:1.2</code>. Pin a tag rather than <code class="mono">latest</code>.</p>
            <label v-if="!isDigestRef" class="mt-2 flex items-start gap-2 text-sm text-zinc-200">
              <input v-model="form.auto_update" type="checkbox" class="check mt-0.5" />
              <span>Redeploy automatically when this tag changes <span class="text-zinc-400">(checked every 10 minutes; new images must pass the size and non-root checks)</span></span>
            </label>
            <details class="mt-2 rounded-md border border-pico-border bg-pico-card px-3 py-2 text-sm">
              <summary class="cursor-pointer text-zinc-200">Image requirements</summary>
              <ul class="mt-2 list-disc space-y-1 pl-5 text-xs text-zinc-300">
                <li>Public: anonymous pulls must work (Docker Hub, GHCR, etc.).</li>
                <li>Must not run as root. Set a non-root <code class="mono">USER</code> in the Dockerfile.</li>
                <li>A named USER (e.g. <code class="mono">USER app</code>) runs as UID 10001, so files it owns may not be writable. A numeric <code class="mono">USER 10001</code> avoids that.</li>
                <li>Up to {{ imageMb }} MB compressed.</li>
              </ul>
            </details>
          </div>

          <div>
            <label for="f-port" class="label">Container port</label>
            <input
              id="f-port"
              v-model="form.port"
              type="number"
              required
              inputmode="numeric"
              aria-describedby="f-port-help"
              :aria-invalid="!!fieldErrors.port || undefined"
              class="input mono !w-32"
              @input="onField('port')"
              @blur="fieldErrors = { ...fieldErrors, port: portError() }"
            />
            <p v-if="fieldErrors.port" class="field-error" role="alert">{{ fieldErrors.port }}</p>
            <p id="f-port-help" class="help">The port your app listens on inside the container, 1024-65535 (8080 is common). Running nginx? Use <code class="mono">nginxinc/nginx-unprivileged</code>, which listens on 8080.</p>
          </div>

          <fieldset>
            <legend class="label">Add-ons</legend>
            <div class="space-y-2">
              <label class="flex items-start gap-2 text-sm text-zinc-200">
                <input v-model="form.want_db" type="checkbox" :disabled="isEdit || dbOff" class="check mt-0.5" />
                <span>
                  Database <span class="text-zinc-400">({{ dbLabel }})</span>
                  <span v-if="dbOff && !isEdit" class="help block !mt-0">Databases aren't available right now.</span>
                </span>
              </label>
              <fieldset v-if="form.want_db" class="ml-6 flex gap-4" role="radiogroup" aria-label="Database engine">
                <label v-for="eng in engineChoices" :key="eng.value" class="flex items-center gap-1.5 text-sm text-zinc-200">
                  <input
                    v-model="form.db_engine"
                    type="radio"
                    name="db_engine"
                    :value="eng.value"
                    :disabled="isEdit || !eng.available"
                    class="check"
                  />
                  <span :class="{ 'text-zinc-500': !eng.available && !isEdit }">{{ eng.label }}</span>
                </label>
                <span v-if="isEdit" class="help !mt-0 self-center">Engine can't be changed after creation.</span>
              </fieldset>
              <label class="flex items-start gap-2 text-sm text-zinc-200">
                <input v-model="form.want_storage" type="checkbox" :disabled="isEdit || storageOff" class="check mt-0.5" />
                <span>
                  Blob storage <span class="text-zinc-400">({{ bucketLabel }})</span>
                  <span class="help block !mt-0">S3-compatible bucket for files your app reads and writes over the network.</span>
                  <span v-if="storageOff && !isEdit" class="help block !mt-0">Blob storage isn't available right now.</span>
                </span>
              </label>
            </div>
            <p class="help">Can't be changed after creation.</p>
          </fieldset>

          <fieldset v-if="volumesOn || volAttached">
            <legend class="label">Persistent volume</legend>
            <p class="help !mt-0 mb-1">A disk mounted into your container that survives restarts.</p>
            <label class="flex items-start gap-2 text-sm text-zinc-200">
              <input v-model="vol.enabled" type="checkbox" :disabled="volAttached" class="check mt-0.5" />
              <span>Add a persistent volume <span v-if="volAttached" class="text-zinc-400">(attached; can't be removed except by deleting the app)</span></span>
            </label>
            <div v-if="vol.enabled" class="ml-6 mt-2 space-y-3">
              <div class="alert-warn text-xs">
                Forces a single replica. Each deploy or restart stops the old pod before the new one starts, so expect about a minute of downtime.
                Volume data is included in backups when the platform has volume backups turned on. It can't be removed except by deleting the app.
              </div>
              <div>
                <label for="vol-size" class="label">Size (GiB)</label>
                <input id="vol-size" v-model="vol.size" type="number" min="1" :max="volMax" inputmode="numeric" class="input mono !w-32" :aria-invalid="!!volSizeError || undefined" />
                <p v-if="volSizeError" class="field-error" role="alert">{{ volSizeError }}</p>
                <p v-else class="help">1 to {{ volMax }} GiB.{{ volAttached ? ' Ask an administrator to grow it.' : '' }}</p>
              </div>
              <div>
                <label for="vol-mount" class="label">Mount path</label>
                <input id="vol-mount" v-model="vol.mount" type="text" maxlength="255" spellcheck="false" class="input mono !w-64" :aria-invalid="!!volMountError || undefined" />
                <p v-if="volMountError" class="field-error" role="alert">{{ volMountError }}</p>
                <p v-else-if="volAttached && vol.mount !== originalMount" class="help">Changing the mount path redeploys the app.</p>
              </div>
            </div>
          </fieldset>

          <div>
            <p class="label">Environment variables</p>
            <EnvVarEditor v-model="env" :want-db="form.want_db" :want-storage="form.want_storage" :errors="envErrors" />
          </div>

          <details class="rounded-md border border-pico-border bg-pico-card px-3 py-2" :open="form.health_check_enabled || !!pathError || !!timeoutError">
            <summary class="cursor-pointer text-sm text-zinc-200">Advanced: health check</summary>
            <div class="mt-3 space-y-3">
              <div>
                <label class="flex items-center gap-2 text-sm text-zinc-200">
                  <input v-model="form.health_check_enabled" type="checkbox" aria-describedby="hc-enabled-help" class="check" />
                  Enable HTTP health check
                </label>
                <p id="hc-enabled-help" class="help">
                  Off: traffic is sent once the port accepts connections. On: the platform calls this path; non-2xx/3xx responses
                  take the app out of rotation and restart it if they persist.
                  <RouterLink to="/guide/user#8-health-checks" target="_blank" class="link">Health check docs</RouterLink>
                </p>
              </div>
              <div>
                <label for="hc-path" class="label">Health check path</label>
                <input
                  id="hc-path"
                  v-model="form.health_check_path"
                  type="text"
                  maxlength="255"
                  :disabled="!form.health_check_enabled"
                  aria-describedby="hc-path-help"
                  :aria-invalid="!!pathError || undefined"
                  class="input mono !w-64"
                />
                <p v-if="pathError" class="field-error" role="alert">{{ pathError }}</p>
                <p id="hc-path-help" class="help">Must start with /. Default /healthz.</p>
              </div>
              <div>
                <label for="hc-timeout" class="label">Timeout (seconds)</label>
                <input
                  id="hc-timeout"
                  v-model="form.health_check_timeout"
                  type="number"
                  min="1"
                  max="90"
                  inputmode="numeric"
                  placeholder="default"
                  :disabled="!form.health_check_enabled"
                  aria-describedby="hc-timeout-help"
                  :aria-invalid="!!timeoutError || undefined"
                  class="input mono !w-32"
                />
                <p v-if="timeoutError" class="field-error" role="alert">{{ timeoutError }}</p>
                <p id="hc-timeout-help" class="help">How long a single probe may take before it counts as failed (max 90s). Blank = default (3s readiness, 5s liveness).</p>
              </div>
            </div>
          </details>
        </fieldset>

        <div ref="errorBox">
          <div v-if="formError" role="alert" class="alert-error">{{ formError }}</div>
        </div>

        <p class="meta sm:hidden">{{ isEdit ? 'Saving redeploys the app.' : 'The app deploys right after you create it.' }}</p>
        <div class="sticky bottom-0 -mx-3 -mb-3 flex items-center justify-between gap-2 rounded-b-lg border-t border-pico-border bg-pico-panel/95 px-3 py-2 backdrop-blur sm:-mx-4 sm:-mb-4 sm:px-4">
          <p class="meta hidden sm:block">{{ isEdit ? 'Saving redeploys the app.' : 'The app deploys right after you create it.' }}</p>
          <div class="flex w-full gap-2 sm:w-auto">
            <RouterLink :to="isEdit && appId !== null ? `/apps/${appId}` : '/apps'" class="btn-secondary flex-1 sm:flex-none">Cancel</RouterLink>
            <button type="submit" class="btn-primary flex-1 sm:flex-none" :disabled="saving || suspended" :aria-busy="saving">
              {{ saving ? busyLabel : isEdit ? 'Save changes' : 'Create app' }}
            </button>
          </div>
        </div>
      </form>

      <section v-if="isEdit && loadedApp" class="mt-6" aria-labelledby="h-other">
        <h2 id="h-other" class="section-title mb-2">Other settings <span class="text-sm font-normal text-zinc-400">(not in the YAML spec)</span></h2>
        <AppSettingsPanel :app="loadedApp" @update:app="onPanelUpdate" />
        <BackupSettings class="mt-3" :app-id="loadedApp.id" :suspended="suspended" :has-data="!!(loadedApp.want_db || loadedApp.want_storage)" />
      </section>
      </template>
    </main>
  </div>
</template>
