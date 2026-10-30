import type { BuildStatus } from '../types'

// Spend as "$0.42"; never rounds a non-zero amount below a cent to "$0.00".
export function fmtUsd(n: number | null | undefined): string {
  const v = n ?? 0
  if (v > 0 && v < 0.01) return '<$0.01'
  return `$${v.toFixed(2)}`
}

export const ACTIVE_BUILD: BuildStatus[] = ['queued', 'running']
export const isActive = (s: string) => (ACTIVE_BUILD as string[]).includes(s)

export function duration(startIso: string, endIso: string | null): string {
  if (!endIso) return ''
  const s = Math.round((new Date(endIso.endsWith('Z') || /[+-]\d\d:?\d\d$/.test(endIso) ? endIso : endIso + 'Z').getTime() - new Date(startIso.endsWith('Z') || /[+-]\d\d:?\d\d$/.test(startIso) ? startIso : startIso + 'Z').getTime()) / 1000)
  if (!isFinite(s) || s < 0) return ''
  return s < 60 ? `${s} s` : `${Math.floor(s / 60)} min ${s % 60} s`
}

export const RUN_KIND_LABEL: Record<string, string> = { build: 'Build', feedback: 'Change', resume: 'Resume' }
