import type { BuildStatus, KaisenOutput, KaisenOutputForm, KaisenOutputPayload } from '../types'

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

export function blankOutputForm(): KaisenOutputForm {
  return { output_mode: 'download', github_repo: '', github_branch: '', github_auth: 'project', github_token: '', clear_github_token: false, push_on_finish: false, open_pr: false }
}
export function outputToForm(o: KaisenOutput): KaisenOutputForm {
  return { output_mode: o.mode, github_repo: o.repo ?? '', github_branch: o.branch ?? '', github_auth: o.auth, github_token: '', clear_github_token: false, push_on_finish: o.push_on_finish, open_pr: o.open_pr }
}
export function outputPayload(f: KaisenOutputForm): KaisenOutputPayload {
  const p: KaisenOutputPayload = {
    mode: f.output_mode, repo: f.github_repo.trim(), branch: f.github_branch.trim(),
    auth: f.github_auth, push_on_finish: f.push_on_finish, open_pr: f.open_pr,
  }
  if (f.github_auth === 'build' && f.github_token) p.github_token = f.github_token
  if (f.github_auth === 'build' && f.clear_github_token) p.clear_github_token = true
  return p
}

// Backend field names (OutputIn) -> form field names, for fieldErrors from PUT .../output and POST /kaisen/builds.
const OUTPUT_FIELD: Record<string, string> = { mode: 'output_mode', repo: 'github_repo', branch: 'github_branch', auth: 'github_auth' }
export function outputFieldErrors(errs: Record<string, string>): Record<string, string> {
  const out: Record<string, string> = {}
  for (const [k, v] of Object.entries(errs)) out[OUTPUT_FIELD[k] ?? k] = v
  return out
}
