import { parseTs } from '../composables/appUi'
import { effectiveZone, fmt, fmtDateTime, zoneAbbr } from './tz'

// Human dates: "Oct 20, 12:10 AM · 2h ago" with the full value in a title attribute.
export function fullDate(iso: string): string {
  const d = parseTs(iso)
  return isNaN(d.getTime()) ? iso : `${fmtDateTime(d)} ${zoneAbbr(d)}`
}

export function ago(iso: string, now = Date.now()): string {
  const t = parseTs(iso).getTime()
  if (isNaN(t)) return ''
  const s = Math.max(0, Math.round((now - t) / 1000))
  if (s < 60) return 'just now'
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  if (s < 86400 * 30) return `${Math.floor(s / 86400)}d ago`
  return ''
}

export function humanDate(iso: string): string {
  const d = parseTs(iso)
  if (isNaN(d.getTime())) return iso
  const base = fmt(d, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })
  const rel = ago(iso)
  return rel ? `${base} · ${rel}` : base
}

export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    return false
  }
}

// Fixed-width local timestamp for table cells (pair with .mono): "2026-10-20 01:07".
export function stamp(iso: string): string {
  const d = parseTs(iso)
  if (isNaN(d.getTime())) return iso
  const g = Object.fromEntries(
    new Intl.DateTimeFormat('en-US', { timeZone: effectiveZone(), hourCycle: 'h23', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
      .formatToParts(d).map((x) => [x.type, x.value]),
  )
  return `${g.year}-${g.month}-${g.day} ${g.hour}:${g.minute}`
}

// Tooltip for a timestamp cell: full local time plus how long ago.
export function stampTitle(iso: string): string {
  const rel = ago(iso)
  return rel ? `${fullDate(iso)} (${rel})` : fullDate(iso)
}
