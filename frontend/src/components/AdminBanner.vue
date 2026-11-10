<script setup lang="ts">
import { computed, onMounted, ref, useId } from 'vue'
import { getAdminBanner, putAdminBanner, type Banner } from '../api/admin'
import { ApiError } from '../api/client'
import { useBannerStore } from '../stores/banner'
import { useToastStore } from '../stores/toast'
import PlatformBanner from './PlatformBanner.vue'

const TITLE_MAX = 80
const TEXT_MAX = 255
const uid = useId()
const toast = useToastStore()
const bannerStore = useBannerStore()
const form = ref<Banner>({ enabled: false, level: 'info', title: '', text: '' })
const saved = ref('')
const loaded = ref(false)
const loadError = ref('')
const saveError = ref('')
const saving = ref(false)

const snap = (b: Banner) => JSON.stringify([b.enabled, b.level, b.title, b.text])
const dirty = computed(() => snap(form.value) !== saved.value)
const valid = computed(() => form.value.title.length <= TITLE_MAX && form.value.text.length <= TEXT_MAX)

onMounted(async () => {
  try {
    const b = await getAdminBanner()
    form.value = { ...b }
    saved.value = snap(b)
  } catch (e) {
    loadError.value = e instanceof ApiError ? e.message : "Couldn't load the banner."
  } finally {
    loaded.value = true
  }
})

async function save() {
  saving.value = true
  saveError.value = ''
  try {
    const b = await putAdminBanner({ ...form.value })
    form.value = { ...b }
    saved.value = snap(b)
    bannerStore.set(b)
    toast.success('Banner saved.')
  } catch (e) {
    saveError.value = e instanceof ApiError ? e.message : "Couldn't save the banner. Try again."
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <section class="sec" :aria-labelledby="`${uid}-h`" data-testid="admin-banner">
    <div class="border-b border-pico-border px-3 py-2">
      <h2 :id="`${uid}-h`" class="section-title">Banner</h2>
    </div>
    <div class="space-y-(--field-gap) px-3 py-3">
      <div v-if="loadError" role="alert" class="alert-error">{{ loadError }}</div>
      <p v-else-if="!loaded" class="text-sm text-zinc-400" role="status">Loading banner…</p>
      <form v-else class="space-y-(--field-gap)" novalidate @submit.prevent="save">
        <p class="help">A one-line notice shown above every page, including the sign-in page, to everyone. Plain text.</p>
        <label class="flex items-center gap-2 text-sm text-zinc-200">
          <input v-model="form.enabled" type="checkbox" class="h-4 w-4 accent-current" />
          <span>Show the banner</span>
        </label>
        <fieldset>
          <legend class="label">Level</legend>
          <div class="flex items-center gap-4 text-sm text-zinc-200">
            <label v-for="o in [{ v: 'info', t: 'Info' }, { v: 'warning', t: 'Warning' }] as const" :key="o.v" class="flex items-center gap-2">
              <input v-model="form.level" type="radio" :name="`${uid}-level`" :value="o.v" class="h-4 w-4 accent-current" />
              <span>{{ o.t }}</span>
            </label>
          </div>
        </fieldset>
        <div>
          <label :for="`${uid}-title`" class="label">Title</label>
          <input :id="`${uid}-title`" v-model="form.title" type="text" class="input" :aria-invalid="form.title.length > TITLE_MAX || undefined" :aria-describedby="`${uid}-title-n`" />
          <p :id="`${uid}-title-n`" class="meta mt-0.5" :class="form.title.length > TITLE_MAX ? 'text-status-error' : ''"><span class="mono">{{ form.title.length }} / {{ TITLE_MAX }}</span></p>
        </div>
        <div>
          <label :for="`${uid}-text`" class="label">Text</label>
          <textarea :id="`${uid}-text`" v-model="form.text" rows="3" class="input" :aria-invalid="form.text.length > TEXT_MAX || undefined" :aria-describedby="`${uid}-text-n`" />
          <p :id="`${uid}-text-n`" class="meta mt-0.5" :class="form.text.length > TEXT_MAX ? 'text-status-error' : ''"><span class="mono">{{ form.text.length }} / {{ TEXT_MAX }}</span></p>
        </div>
        <div>
          <p class="label">Preview</p>
          <PlatformBanner v-if="form.title || form.text" :banner="form" slim data-testid="banner-preview" />
          <p v-else class="help">Nothing to show yet.</p>
          <p v-if="!form.enabled && (form.title || form.text)" class="help">Not shown to anyone until "Show the banner" is on.</p>
        </div>
        <div v-if="saveError" role="alert" class="alert-error">{{ saveError }}</div>
        <div class="flex flex-wrap items-center gap-3">
          <button type="submit" class="btn-primary" :disabled="saving || !dirty || !valid" :aria-busy="saving">{{ saving ? 'Saving…' : 'Save banner' }}</button>
          <span v-if="dirty" class="meta">Unsaved changes</span>
        </div>
      </form>
    </div>
  </section>
</template>
