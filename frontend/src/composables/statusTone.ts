// One place that maps a status string to the four state tones of the design system (ok, warn, err, off) and to the
// label shown next to the dot. Used by StatusBadge and the sidebar tree so they never disagree.
export type Tone = 'ok' | 'warn' | 'err' | 'off'

const APP: Record<string, { tone: Tone; label: string }> = {
  running: { tone: 'ok', label: 'Running' },
  active: { tone: 'ok', label: 'Active' },
  deploying: { tone: 'warn', label: 'Deploying' },
  pending: { tone: 'warn', label: 'Queued' },
  retrying: { tone: 'warn', label: 'Retrying' },
  error: { tone: 'err', label: 'Failed' },
  suspended: { tone: 'off', label: 'Suspended' },
  stopped: { tone: 'off', label: 'Stopped' },
  revoked: { tone: 'off', label: 'Revoked' },
}

// Kaisen build statuses: "running" would clash with an app's Running.
const BUILD: Record<string, { tone: Tone; label: string }> = {
  idle: { tone: 'off', label: 'Not built' },
  queued: { tone: 'warn', label: 'Queued' },
  running: { tone: 'warn', label: 'Building' },
  waiting: { tone: 'warn', label: 'Needs you' },
  done: { tone: 'ok', label: 'Ready' },
  error: { tone: 'err', label: 'Failed' },
  stopped: { tone: 'off', label: 'Stopped' },
}

export function statusLook(status: string, kind: 'app' | 'build' = 'app'): { tone: Tone; label: string } {
  const hit = (kind === 'build' ? BUILD : APP)[status]
  if (hit) return hit
  return { tone: 'off', label: status ? status[0].toUpperCase() + status.slice(1) : 'Unknown' }
}

// In-flight states get a gentle pulse (motion-safe) on the dot.
export const isBusyStatus = (status: string, kind: 'app' | 'build' = 'app') =>
  statusLook(status, kind).tone === 'warn' && status !== 'pending' && status !== 'queued' && status !== 'waiting'
