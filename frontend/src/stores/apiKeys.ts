import { defineStore } from 'pinia'
import { ref } from 'vue'
import { createApiKey, listApiKeys, revokeApiKey } from '../api/keys'
import type { ApiKey } from '../types'

export const useApiKeysStore = defineStore('apiKeys', () => {
  const keys = ref<ApiKey[]>([])
  const loaded = ref(false)

  async function load() {
    keys.value = (await listApiKeys()) ?? []
    loaded.value = true
  }

  // Returns the full create response (including the one-time plaintext key)
  // to the caller; only the non-secret fields go into the list.
  async function create(name?: string) {
    const created = await createApiKey(name)
    keys.value = [
      { id: created.id, prefix: created.prefix, created_at: created.created_at, revoked_at: null, name: created.name ?? name ?? null },
      ...keys.value,
    ]
    return created
  }

  async function revoke(id: number) {
    await revokeApiKey(id)
    const now = new Date().toISOString()
    keys.value = keys.value.map((k) => (k.id === id ? { ...k, revoked_at: k.revoked_at ?? now } : k))
  }

  return { keys, loaded, load, create, revoke }
})
