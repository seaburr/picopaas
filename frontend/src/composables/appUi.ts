import { onBeforeUnmount, ref } from 'vue'
import type { App } from '../types'
import { fmtDateTime } from '../utils/tz'

// Display name for a database engine ('mysql' | 'pg'). One source of truth for every DB label in the UI.
export const DB_ENGINE_LABEL: Record<string, string> = { mysql: 'MySQL', pg: 'PostgreSQL' }
export const dbEngineLabel = (engine?: string | null): string => DB_ENGINE_LABEL[engine ?? 'mysql'] ?? 'MySQL'

// Backend datetimes may be naive UTC ("2026-10-19T12:00:00"); treat a missing zone as UTC.
export function parseTs(iso: string): Date {
  const hasZone = /(Z|[+-]\d\d:?\d\d)$/.test(iso)
  return new Date(hasZone ? iso : iso + 'Z')
}

// Shared ticking clock so relative times update without a refetch.
const nowMs = ref(Date.now())
let users = 0
let tick: ReturnType<typeof setInterval> | null = null
export function useNow() {
  users++
  if (!tick) tick = setInterval(() => (nowMs.value = Date.now()), 15000)
  onBeforeUnmount(() => {
    if (--users === 0 && tick) {
      clearInterval(tick)
      tick = null
    }
  })
  return nowMs
}

function span(sec: number): string {
  const s = Math.abs(sec)
  if (s < 60) return `${Math.max(1, Math.round(s))} sec`
  if (s < 3600) return `${Math.round(s / 60)} min`
  if (s < 86400) return `${Math.round(s / 3600)} h`
  return `${Math.round(s / 86400)} d`
}

export function relTime(iso: string, now = Date.now()): string {
  const d = parseTs(iso)
  if (isNaN(d.getTime())) return iso
  const sec = (d.getTime() - now) / 1000
  return sec >= 0 ? `in ${span(sec)}` : `${span(sec)} ago`
}

// "Restarted 3 min ago: your app ran out of memory (limit 384 MB)." from an event message "Restarted: ...";
// other events (e.g. a failed backup) read "3 min ago: ...".
export function eventNote(ev: { at: string; message: string }, now = Date.now()): string {
  const m = ev.message.match(/^Restarted:\s*(.*)$/s)
  if (m) return `Restarted ${relTime(ev.at, now)}: ${m[1]}`
  const when = relTime(ev.at, now)
  return `${when.charAt(0).toUpperCase()}${when.slice(1)}: ${ev.message}`
}

export function fullTime(iso: string): string {
  const d = parseTs(iso)
  return isNaN(d.getTime()) ? iso : fmtDateTime(d)
}

export function retryText(iso: string, now = Date.now()): string {
  const d = parseTs(iso)
  if (isNaN(d.getTime())) return ''
  return d.getTime() - now < 5000 ? 'Retrying now' : `Retrying ${relTime(iso, now)}`
}

export type FailAction = 'edit' | 'logs' | 'restart'
export interface Failure {
  reason: string
  text: string
  actions: FailAction[]
}

// Raw k8s strings from older backends -> machine reason.
function guessReason(detail: string | null | undefined): string {
  const d = detail ?? ''
  if (/ImagePull|ErrImage|pull/i.test(d)) return 'image_pull'
  if (/CrashLoop/i.test(d)) return 'crash_loop'
  if (/OOM/i.test(d)) return 'oom'
  if (/readiness|not ready|probe/i.test(d)) return 'not_ready'
  if (/quota/i.test(d)) return 'quota'
  if (/root/i.test(d)) return 'root_image'
  return 'unknown'
}

export function failureInfo(app: App, memLimitMi = 384): Failure {
  const reason = app.status_reason || guessReason(app.status_detail)
  const mem = app.resources?.mem_limit_mi ?? memLimitMi
  const known: Record<string, { text: string; actions: FailAction[] }> = {
    image_pull: { text: "Can't pull the image. Check the name and tag, and that the image is public.", actions: ['edit'] },
    crash_loop: {
      text: `Your app starts, then exits. Check that it listens on port ${app.port} and isn't missing an env var.`,
      actions: ['logs', 'restart'],
    },
    oom: { text: `Out of memory (limit ${mem} MB). Reduce the app's memory use.`, actions: ['logs', 'restart'] },
    not_ready: {
      text: `Port ${app.port} isn't answering. Is that the port your app listens on?`,
      actions: ['edit', 'logs'],
    },
    root_image: { text: "The image runs as root, which isn't supported. Use an image with a non-root USER.", actions: ['edit'] },
  }
  const k = known[reason]
  // Backend status_detail is a human sentence once status_reason is set; prefer it, fall back to our copy.
  const backendHuman = app.status_reason && app.status_detail && /\s/.test(app.status_detail)
  const text = backendHuman ? app.status_detail! : (k?.text ?? app.status_detail ?? 'The app could not be started.')
  return { reason, text, actions: k?.actions ?? ['logs', 'restart'] }
}

export const SAMPLE_APP = { name: 'podinfo', image: 'ghcr.io/stefanprodan/podinfo:6.15.0', port: 9898 }

// Hosts the app answers on, best first: active custom domain, reserved platform subdomain, generated host.
// A pending/error custom domain isn't serving, so it is never listed here.
export function servedHosts(app: Pick<App, 'hostname' | 'platform_host' | 'custom_domain'>): string[] {
  const cd = app.custom_domain
  const all = [cd && cd.status === 'active' ? cd.host : null, app.platform_host, app.hostname]
  return all.filter((h, i): h is string => !!h && all.indexOf(h) === i)
}

export function primaryHost(app: Pick<App, 'hostname' | 'platform_host' | 'custom_domain'>): string {
  return servedHosts(app)[0] ?? app.hostname
}
