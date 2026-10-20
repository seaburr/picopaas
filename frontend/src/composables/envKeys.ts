export const ENV_KEY_RE = /^[A-Z_][A-Z0-9_]*$/

export function envKeyError(key: string): string {
  if (!key) return 'Name is required.'
  if (!ENV_KEY_RE.test(key)) return "Use UPPER_SNAKE_CASE (letters, digits, underscore; can't start with a digit)."
  return ''
}

// Suggested env var name for each reference chip.
export const SUGGESTED_KEYS: Record<string, string> = {
  'db.url': 'DATABASE_URL',
  'db.host': 'DB_HOST',
  'db.port': 'DB_PORT',
  'db.name': 'DB_NAME',
  'db.user': 'DB_USER',
  'db.password': 'DB_PASSWORD',
  'db.ca_cert': 'DB_CA_CERT',
  'db.tls': 'DB_TLS',
  'storage.endpoint': 'S3_ENDPOINT',
  'storage.region': 'S3_REGION',
  'storage.bucket': 'S3_BUCKET',
  'storage.access_key': 'S3_ACCESS_KEY',
  'storage.secret_key': 'S3_SECRET_KEY',
}

export const REF_TIPS: Record<string, string> = {
  'db.url': 'Full connection string',
  'db.ca_cert': 'CA certificate for the TLS connection',
  'db.tls': 'Whether TLS is required',
}

export const refLabel = (ref: string) => '${{ ' + ref + ' }}'

// Every ${{ x.y }} reference used in a value.
export function refsIn(value: string): string[] {
  return [...value.matchAll(/\$\{\{\s*([a-z_.]+)\s*\}\}/g)].map((m) => m[1])
}
