import type { AdviceSeverity, OwnerKey } from '../types'

export const SEVERITY: Record<AdviceSeverity, { word: string; tone: 'err' | 'warn' | 'off' }> = {
  critical: { word: 'Critical', tone: 'err' },
  warning: { word: 'Warning', tone: 'warn' },
  info: { word: 'Info', tone: 'off' },
}

// The one place that maps a finding's `field` to the owner resource-change payload key (PATCH /apps/{id}
// `resources`). Adjust here if the backend's field names differ.
const FIELDS: Record<string, { key: OwnerKey; label: string }> = {
  mem_limit_mi: { key: 'mem_limit_mi', label: 'Memory limit' },
  memory_limit_mi: { key: 'mem_limit_mi', label: 'Memory limit' },
  mem_limit_mb: { key: 'mem_limit_mi', label: 'Memory limit' },
  cpu_limit_m: { key: 'cpu_limit_m', label: 'CPU limit' },
  ephemeral_limit_mi: { key: 'ephemeral_limit_mi', label: 'Scratch space limit' },
  storage_quota_mb: { key: 'storage_quota_mb', label: 'Storage quota' },
}
export const resourceField = (field: string) => FIELDS[field] ?? null
