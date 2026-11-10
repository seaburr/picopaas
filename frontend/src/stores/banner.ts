import { defineStore } from 'pinia'
import { ref } from 'vue'
import { apiFetch } from '../api/client'
import type { Banner } from '../api/admin'

// Public platform banner, fetched once on app load. Nothing renders until it has loaded, and nothing on error.
export const useBannerStore = defineStore('banner', () => {
  const banner = ref<Banner | null>(null)
  async function load() {
    try {
      const b = await apiFetch<Banner>('/api/banner')
      banner.value = b?.enabled && (b.title || b.text) ? b : null
    } catch {
      banner.value = null
    }
  }
  // After an admin saves, show the new banner without a reload.
  function set(b: Banner) {
    banner.value = b.enabled && (b.title || b.text) ? b : null
  }
  return { banner, load, set }
})
