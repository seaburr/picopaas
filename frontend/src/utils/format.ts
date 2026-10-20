// Human dates: "Oct 20, 12:10 AM · 2h ago" with the full value in a title attribute.
export function fullDate(iso: string): string {
  const d = new Date(iso)
  return isNaN(d.getTime()) ? iso : d.toLocaleString()
}

export function ago(iso: string, now = Date.now()): string {
  const t = new Date(iso).getTime()
  if (isNaN(t)) return ''
  const s = Math.max(0, Math.round((now - t) / 1000))
  if (s < 60) return 'just now'
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  if (s < 86400 * 30) return `${Math.floor(s / 86400)}d ago`
  return ''
}

export function humanDate(iso: string): string {
  const d = new Date(iso)
  if (isNaN(d.getTime())) return iso
  const base = d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })
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
  const d = new Date(iso)
  if (isNaN(d.getTime())) return iso
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

// Tooltip for a timestamp cell: full local time plus how long ago.
export function stampTitle(iso: string): string {
  const rel = ago(iso)
  return rel ? `${fullDate(iso)} (${rel})` : fullDate(iso)
}
