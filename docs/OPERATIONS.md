# Operations

Moved: generic administration is in [ADMIN_GUIDE.md](ADMIN_GUIDE.md); this instance's runbooks, keys, DR and accepted risks are in [deployment/](deployment/README.md); admin routes are in [API.md](API.md).

## Restoring the platform database

The platform DB is backed up daily into restic repo `s3:<endpoint>/<BACKUP_BUCKET>/<BACKUP_PREFIX>platform/db` (app/platform_backup.py; Admin > Health > Backups). You need the backup bucket key (GitHub secret `BACKUP_*` / the `picopaas` Secret) and the repo password: the Fernet-decrypted value of `platform_settings.platform_backup_repo_password`. If the platform DB is gone, that row is gone with it, so keep the repo password somewhere outside the database (a password manager) once: read it while the platform is up with `python -c "from app import crypto; ..."` against that row.

1. Stop writers: `kubectl -n picopaas-system scale deployment/picopaas --replicas=0`.
2. From a machine with `restic` and `mariadb`: `export AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... RESTIC_REPOSITORY=s3:<endpoint>/<bucket>/<prefix>platform/db RESTIC_PASSWORD=...`; `restic snapshots` and pick one (`latest` is fine).
3. `restic dump latest db.sql > platform.sql` (the dump is stored as one plain SQL file named `db.sql`; confirm with `restic ls latest`).
4. Load into an empty database (create a fresh one rather than overwriting a live one): `mariadb -h <host> -P <port> -u <admin> -p --ssl-ca=ca.pem <dbname> < platform.sql`.
5. Point `DATABASE_URL` at it if it is a new database, run `alembic upgrade head` (the dump may predate the newest migration), then scale the deployment back up.
6. Check login, Admin > Health, and that tenant apps still reconcile. Backups of tenant apps are unaffected (their repos are separate).

## Backups stuck or leaving leftovers

The leader sweeps every 10 minutes (`app/backup_sweeper.py`): see `sweeper` in `GET /admin/health/backups` (`last_run_at`, `cleaned`, `errors`, `orphan_repos`) and the `backup.orphan_cleaned` audit rows. A run that stays `running` past its deadline, an orphan `bk-*` Job/Secret in `picopaas-backup` or `app-*`, or a stale restic lock should clear itself within one sweep; if `errors` is not empty or `last_run_at` is old, check that the leader runs with `RECONCILER_ENABLED=1` and the backup key can list the bucket. `orphan_repos` are repo prefixes with no app config: inspect, never delete automatically. The `restic unlock` step needs the backup image republished and `BACKUP_IMAGE` re-pinned.

## Kaisen runtime image and worker

- **Runtime image**: `images/kaisen-runtime/` (Dockerfile, `boot.py`, `smoke-local.sh` for a real project bundle, `ci-smoke.sh` for CI). `.github/workflows/kaisen-runtime-image.yml` builds it on changes to that folder (or by hand), smoke-tests a static bundle served from the runner (expects HTTP 200), and pushes `ghcr.io/seaburr/picopaas-kaisen-runtime:<sha>` and `:latest`. One-time: make the package Public (GitHub > Packages > picopaas-kaisen-runtime).
- **Re-pin** (needed after every republish you want live; the platform reads `KAISEN_RUNTIME_IMAGE` from `picopaas-env`, digest-pinned exactly like `BACKUP_IMAGE`): run the `bootstrap` workflow (optional input `kaisen_runtime_image_digest`, else it resolves `:latest`). Bootstrap needs Chas's approval to run. If the package does not exist yet the run warns and leaves `KAISEN_RUNTIME_IMAGE` empty (Kaisen deploys stay off). Existing preview apps keep their old image until redeployed.
- **Worker** `kaisen-worker` (`deploy/platform/kaisen-worker.yaml`, applied by `deploy.yml` with every platform image): 1 replica, `Recreate`, 50m/128Mi requests, 250m/384Mi limits, same image and `picopaas-env` as the API pods but `KAISEN_WORKER=1`, `RECONCILER_ENABLED=0`, no Service/Ingress, own ServiceAccount with no token. Egress NetworkPolicy `kaisen-worker-egress`: DNS, managed DB CIDR (3306/25060), internet 443 (private ranges excluded), Traefik pods (probes of the public URL). If a call is blocked, add the narrowest rule there.
- **Heartbeat contract**: the worker touches `/tmp/kaisen-worker-heartbeat` every loop (including while idle and during long model streams, at least every 60 s); the liveness probe restarts the pod when the file is missing or older than 120 s (first check after 90 s).
- **Rollback**: `kubectl -n picopaas-system scale deployment/kaisen-worker --replicas=0` (builds pause, API unaffected) or delete the Deployment/NetworkPolicy; revert the commit to stop `deploy.yml` re-creating it.

## Kaisen worker

`kaisen-worker` (namespace `picopaas-system`) runs Kaisen builds; image and manifest details are in "Kaisen runtime image and worker" above.

- **Heartbeat.** It touches `KAISEN_HEARTBEAT_FILE` (default `/tmp/kaisen-worker-heartbeat`) every loop and every 30 s from a side thread, even during a long model call. Liveness restarts the pod when the file is older than 120 s.
- **Logs.** `kubectl -n picopaas-system logs deploy/kaisen-worker`. Lines use the platform format (logger `app.kaisen.worker`); keys and tokens are not logged.
- **Lease.** Only one worker works at a time, holding the MySQL lock `picopaas-kaisen-worker`. If a build sits in `queued`, check that the pod is Ready and holds the lease (the log says "worker started" after it does).
- **Restart.** `kubectl -n picopaas-system rollout restart deployment/kaisen-worker`. A step in flight is picked up again; a run left `running` is retried.
- **Pause.** Scale to 0 (see Rollback above). Builds stay queued.
- **Known gap.** The deploy check reads status and an HTTP probe only; app logs are not yet passed to the builder (`(logs are not available yet)`).


## Custom 503 page (platform down)

When the API has no ready pods, Traefik would answer with its bare "no available server". Instead the `errors` middleware swaps any 502/503/504 for a static, on-brand page (status code kept) served by `error-pages` (2 tiny busybox httpd replicas in `picopaas-system`, no ServiceAccount token, no dependency on the API or DB; ~10m CPU / 16Mi total, no new paid resource). Sources: `deploy/error-pages/{platform,app}.html` (inline CSS and logo, no external assets) and `deploy/error-pages/error-pages.yaml` (Deployment, Service, two `Middleware` CRs). `deploy.yml` and `bootstrap.yml` build the ConfigMap from the HTML files and apply the manifest BEFORE `deploy/platform/ingress.yaml`; the order matters because an Ingress that names a missing Middleware loses its router (404).

- Wired now: the `picopaas` Ingress (platform.picopaas.xyz and the apex) via annotation `traefik.ingress.kubernetes.io/router.middlewares: picopaas-system-error-pages-platform@kubernetescrd`. The `*.picopaas.xyz` catch-all 404 ingress and the platform-served "suspended" page (a 503 from the API itself) are deliberately NOT wrapped.
- Not wired yet (phase 2, tenant apps, `app.html` "This app isn't running"): needs `providers.kubernetesCRD.allowCrossNamespace: true` in `deploy/traefik/values.yaml` (helm upgrade via bootstrap) plus adding annotation `picopaas-system-error-pages-app@kubernetescrd` in `ingress_manifest()` / `custom_domain_ingress_manifest()` (app/k8s_manifests.py) and a reconcile of existing Ingresses. Side effect: an app's own 502-504 responses would also be replaced by the page. Do NOT use a Traefik entrypoint-level middleware instead: it would also replace the suspended 503 page.
- Edit a page: change the HTML, push; deploy.yml re-creates the ConfigMap, then `kubectl -n picopaas-system rollout restart deployment/error-pages` (ConfigMap mounts refresh in ~1 min without restart, but the restart is deterministic).
- Test (rollout first, then): `kubectl -n picopaas-system scale deployment/picopaas --replicas=0`; `curl -si https://platform.picopaas.xyz/ | head -20` must show `HTTP/2 503` and the PicoPaaS page, in a browser too; then `scale --replicas=2` (the next deploy also restores it). Single-replica kill: `kubectl delete pod -l app=picopaas` leaves the other replica serving. Error pages themselves: `kubectl -n picopaas-system port-forward svc/error-pages 8081:80` then open `/platform.html` and `/app.html`.
- If the platform host ever shows a 404 instead of the app: the Middleware is missing; `kubectl -n picopaas-system get middleware` and apply `deploy/error-pages/error-pages.yaml`.

## Reading platform logs

Every platform line carries a UTC timestamp, level, logger, thread and the call site (`app/logging_setup.py`):

```
2026-10-29T14:03:12.345Z WARNING app.reconciler [ThreadPoolExecutor-0_0] reconciler.py:123 reconcile_app() app 7 failed
2026-10-29T14:03:12.400Z INFO    access 10.150.0.57:51832 "GET /healthz HTTP/1.1" 200
```

Set `LOG_LEVEL=DEBUG` on the platform deployment for more detail (default `INFO`). `apscheduler`, `httpx` and `httpcore` stay at
`WARNING` regardless, because they log every 15 s job run and every request URL. Admin > Health > Logs still redacts secrets
before showing or downloading lines.
