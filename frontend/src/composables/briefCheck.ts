// Deterministic brief check for New build. No model, no network. Warnings only; they never block Start.
// Kaisen limits (app/kaisen/prompts/builder.md): one web process, no background workers or cron, no email sending.
export interface BriefWarning { id: string; text: string }

// "Out of scope: ..." and "no email" / "without cron" are statements of what is NOT wanted.
function wanted(brief: string): string {
  return brief
    .replace(/out of scope\b[^]*$/i, ' ')
    .replace(/\b(no|not|without|never|nothing)\s+(\w+\s+){0,2}?(e-?mail\w*|cron\w*|smtp|background\s+(jobs?|workers?)|scheduled\s+jobs?)/gi, ' ')
}

const DB = /\b(my\s?sql|postgres(ql)?|database|sql)\b/i
const BUCKET = /\b(bucket|s3|object storage)\b/i
const DISK = /\b(sqlite|persistent (disk|volume)|volume)\b/i
const BROWSER = /\b(localstorage|indexeddb|in the browser|browser'?s? (storage|only)|static (page|site)|no server|nothing (is )?saved|nothing saved)\b/i
const UPLOADS = /\b(uploads?|uploaded|attachments?|attach files?|saved files?|file storage)\b/i
const SAVES = /\b(saves?|saved|stores?|stored|persist\w*|accounts?|log ?in|sign ?up|users? (can )?sign)\b/i
const SERVER_NEEDED = /\b(my\s?sql|postgres(ql)?|database|login|sign ?up|api)\b/i
const STATIC = /\b(static (page|site)|no server|without a server)\b/i

const UNSUPPORTED: { id: string; re: RegExp; text: string }[] = [
  { id: 'email', re: /\b(send(s|ing)?|deliver\w*)\b[^.]{0,40}\be-?mails?\b|\be-?mail (notification|alert|reminder|digest|verification)s?\b|\bsmtp\b|\bnewsletters?\b|\bpassword reset (by|via) e-?mail\b/i, text: 'Kaisen apps cannot send email. Remove it or keep it out of the build.' },
  { id: 'cron', re: /\b(cron\w*|scheduled (jobs?|tasks?)|background (jobs?|workers?|tasks?)|job queues?|every (night|hour|day|minute)|nightly|hourly)\b/i, text: 'Previews run one web process: no cron, scheduled jobs or background workers.' },
]

export function checkBrief(brief: string): BriefWarning[] {
  const text = brief.trim()
  if (!text) return []
  const w = wanted(text)
  const out: BriefWarning[] = []
  const db = DB.test(w), bucket = BUCKET.test(w), disk = DISK.test(w), browser = BROWSER.test(text)
  const named = db || bucket || disk || browser
  if (UPLOADS.test(w) && !db && !bucket && !disk) out.push({ id: 'uploads-storage', text: 'It mentions uploads or saved files but names no place to keep them. Say "a storage bucket", MySQL, PostgreSQL or a persistent disk.' })
  if (SAVES.test(w) && !named) out.push({ id: 'no-storage', text: 'Nothing says where data is kept. Name MySQL, PostgreSQL, a bucket, a disk, or "saved in the browser" / "nothing saved".' })
  if (/\b(database|my\s?sql|postgres(ql)?)\b/i.test(w) && !/\b(my\s?sql|postgres(ql)?)\b/i.test(w)) out.push({ id: 'db-engine', text: 'It asks for a database without an engine. Say MySQL or PostgreSQL.' })
  if (STATIC.test(text) && SERVER_NEEDED.test(w.replace(STATIC, ' '))) out.push({ id: 'static-conflict', text: 'It says static or no server but also asks for a database, login or API, which need a server.' })
  for (const u of UNSUPPORTED) if (u.re.test(w)) out.push({ id: u.id, text: u.text })
  return out
}
