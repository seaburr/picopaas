# Runbooks

Instance runbooks for `picopaas.xyz` with real names. Generic procedures are in the [Admin guide](../ADMIN_GUIDE.md);
the manual bring-up path is in `deploy/README.md`.

## Platform key

`PICOPAAS_FERNET_KEY` (GitHub repo secret) is the only platform key. It encrypts every secret stored in MySQL (env-var
values, app DB passwords, storage secret keys, DB server admin passwords). The JWT signing key is derived from it
(HMAC-SHA256 of a fixed label, see `app/config.py`), so rotating it also logs everyone out. An explicit `SECRET_KEY` env
is still honoured but not needed.

Why it matters: the 2026-10-19 cluster replacement regenerated the key per cluster and orphaned the data. Now
`bootstrap` takes the key only from the repo secret and fails if it is missing. GitHub secrets are write-only, so keep
your own copy in a password manager (open to-do in [QUESTIONS.md](../QUESTIONS.md)). With a wrong key, apps with
unreadable values go to **Failed** without stopping the reconciler; `bootstrap_db_server` repairs the `primary` DB
server admin password automatically.

### Rotate the key

1. Generate: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`; save it in
   your password manager.
2. Set it as repo secret `PICOPAAS_FERNET_KEY`.
3. Run the `bootstrap` workflow. It reads the current `FERNET_KEY` from the existing `picopaas-env` Secret and, if it
   differs, stores it (plus earlier previous keys, deduped, max 3) in `FERNET_KEYS_PREVIOUS`. If the existing Secret
   cannot be read (anything but NotFound) bootstrap fails rather than lose the old key.
4. On startup the platform decrypts with the new key then the previous keys (`MultiFernet`) and re-encrypts every
   stored ciphertext (log line `fernet rotation: rotated=... unchanged=... skipped_undecryptable=...`, counts only).
5. Once `rotated` is 0 on a later start and nothing is skipped, previous keys are no longer needed. Bootstrap trims them
   to 3; there is no automatic removal beyond that.

DO token or Spaces keys: update the GitHub secret, then run `bootstrap` (rewrites `picopaas-env`).

## Replace the cluster

What we did on 2026-10-19, with the lessons baked in:

1. `terraform` workflow creates the new DOKS cluster (and MySQL/VPC if needed). "Insufficient capacity" means the node
   size is not currently offered in the region; the workflow pre-check catches it early, so pick a size from the list.
2. `bootstrap` workflow. Things that went wrong the first time and are now handled:
   - **Orphaned load balancer.** The old cluster's LB keeps the name `picopaas` (`deploy/traefik/values.yaml`), so the
     new Traefik Service could not claim it. Bootstrap deletes any LB named `picopaas` that is not tagged for the
     current cluster and not on the live Service IP, before installing Traefik. On a name clash, delete the stale LB in
     the DO panel and re-run.
   - **Keys.** The new cluster used freshly generated keys. Keys now come from the `PICOPAAS_*` repo secrets; do not
     delete them.
   - **DB server creds.** The `primary` DbServer row is kept in sync with what bootstrap reads from DO each run.
3. The platform starts; the reconciler recreates every `running` app whose namespace is missing (the DR sweep), from
   the apps, env vars and DB/bucket rows in the managed MySQL platform database, which survives the cluster.
4. Verify: `/healthz`, log in, `/admin/health`, open an app. Use `diag` for logs.
5. If the managed MySQL was also lost, restore from DO backups. The in-cluster variant uses the nightly dump described
   in `deploy/README.md` ("MySQL restore").

DNS: bootstrap upserts only the A records `*` and `picopaas`.

## Caps change (2026-10-19)

Caps are defined once in `app/config.py` (env overrides `DB_QUOTA_BYTES`, `STORAGE_QUOTA_BYTES`, `MAX_IMAGE_SIZE_BYTES`,
`DEFAULT_CPU_LIMIT_M`, `DEFAULT_MEM_LIMIT_MI`). Existing apps picked up new limits via the rendered-hash sweep. Databases
over 50 MB went read-only at the next quota poll and buckets over 100 MB within 5 minutes; see the User Guide for how
owners free space.

## Open issues

GitHub issues (`gh issue list`):

| # | Title |
|---|---|
| 10 | Move the API under `/api/` (the SPA and API share paths) |
| 11 | In-cluster calls to an app's public URL time out (load balancer hairpin) |
| 9 | `status_detail` "deployment failed" is too vague (messages since rewritten; may be ready to close) |

Other known gaps: one node, so recycling causes brief downtime and both platform replicas share it; the platform's
ClusterRole can read Secrets in all namespaces (`deploy/README.md`, "Known RBAC exposure"); PodDisruptionBudget for the
platform is deferred until 2+ nodes; a failed MySQL `DROP` or bucket delete during app deletion is logged and ignored,
so provider-side orphans are possible.

## Accepted risks (PostgreSQL pentest)

Accepted by Chas, 2026-10-22. None gives access to another tenant's data.

- **F1: shared database subnet.** Tenant DB egress allows the whole VPC `/24` on the database port. A PostgreSQL app can
  open TCP to the MySQL server, and a MySQL app whose server listens on 25060 can open TCP to the PostgreSQL server.
  Credentials and CONNECT are still needed, and the platform admin logins refuse tenants. To tighten, narrow egress to
  each DbServer's `/32`, which means pinning the managed IPs.
- **F2: trusted extensions on PostgreSQL.** A tenant owns schema `public`, so it can `CREATE EXTENSION dblink` or
  `postgres_fdw` and open outbound TCP from the database server. These still need a password plus CONNECT on the target.
  DO blocks `file_fdw`, `COPY ... PROGRAM`, file reads and untrusted procedural languages. Revoking CREATE on `public`
  would break app migrations.
- **F3: catalog names (open, see QUESTIONS.md).** Tenants can read other apps' database and role names from
  `pg_database` and `pg_roles`, not data or password hashes. This cannot be sealed on a shared server.

Details: [PENTEST.md](../PENTEST.md).

## Operator tools

- **Ghost blog** (`blog.picopaas.xyz`, app 98, reserved subdomain `blog`): branding in `deploy/ghost/`, applied through
  the Ghost Admin API; see `deploy/ghost/README.md`.
- **Platform host for an app:** `PUT /admin/apps/{id}/platform-host` with `{"subdomain":"blog"}`.

## Admin recovery

Generic steps are in [ADMIN_GUIDE.md](../ADMIN_GUIDE.md#break-glass-recovery). On this instance:

```bash
kubectl -n picopaas-system exec -it deploy/picopaas -- python -m app.manage list-admins
kubectl -n picopaas-system exec -it deploy/picopaas -- python -m app.manage grant-admin <email>
```

## WordPress restore rehearsal on throwaway app 152 (2026-10-23)
Restored a real blog backup (DB dump with audit-log data stripped, 65 MB wp-content tarball, small extra static folder) onto app 152
(`wordpress`, image `ghcr.io/seaburr/picopaas-example-wordpress:afefe81`, 1 GiB volume at `/var/www/html`, managed MySQL, bucket quota raised
to 150 MB, CPU 500m) using `WP_RESTORE=1` and the REQ-8 presigned upload (`picopaas storage upload`). Outcome: restore completed on the first
try (about two minutes from redeploy to serving), posts, theme, uploads and the static download folder all 200, install.php 403, restart skipped
the restore (marker) and uploads persisted. Bundle objects deleted afterwards (presigned GET returned 404) and `WP_RESTORE` unset. Findings:
one abandoned PHP 7-era plugin fatals on PHP 8.5 (deactivated by `WP_RESTORE_DEACTIVATE`); a restart during a background plugin auto-update
left a `.maintenance` flag (site showed "Briefly unavailable" until WordPress's own 10-minute expiry); fixed in the working tree by disabling background
updaters by default and clearing the flag at boot (needs a new image publish). Presigned PUT size cap verified on real Spaces: 20-byte body against
a 10-byte signature returned HTTP 403 SignatureDoesNotMatch. App 152 stays up until Chas deletes it.
