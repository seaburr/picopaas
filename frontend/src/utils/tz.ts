import { ref } from 'vue'

// Single source of truth for display time zone. The auth store keeps `userZone` in sync with user.timezone;
// null = use the browser zone. Formatters read `effectiveZone()` so templates re-render when it changes.
export const userZone = ref<string | null>(null)

export const browserZone = (): string => Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'

export function effectiveZone(): string {
  const z = userZone.value
  if (z) {
    try {
      new Intl.DateTimeFormat(undefined, { timeZone: z })
      return z
    } catch {
      /* unknown zone on this browser: fall through */
    }
  }
  return browserZone()
}

export function listZones(): string[] {
  const f = (Intl as unknown as { supportedValuesOf?: (k: string) => string[] }).supportedValuesOf
  const zs = f ? f('timeZone') : ['UTC', 'America/New_York', 'America/Chicago', 'America/Denver', 'America/Los_Angeles', 'Europe/London', 'Europe/Berlin', 'Asia/Kolkata', 'Asia/Tokyo', 'Australia/Sydney']
  return zs.includes('UTC') ? zs : ['UTC', ...zs]
}

// Format a Date in the effective zone.
export const fmt = (d: Date, opts: Intl.DateTimeFormatOptions): string =>
  new Intl.DateTimeFormat(undefined, { ...opts, timeZone: effectiveZone() }).format(d)

export const fmtDateTime = (d: Date): string => fmt(d, { dateStyle: 'medium', timeStyle: 'medium' })
export const fmtTime = (d: Date): string => fmt(d, { timeStyle: 'medium' })

// Short zone name ("EDT") for tooltips.
export function zoneAbbr(d: Date): string {
  return new Intl.DateTimeFormat(undefined, { timeZone: effectiveZone(), timeZoneName: 'short' })
    .formatToParts(d).find((p) => p.type === 'timeZoneName')?.value ?? ''
}
