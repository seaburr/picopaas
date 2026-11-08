# Operations

Moved: generic administration is in [ADMIN_GUIDE.md](ADMIN_GUIDE.md); this instance's runbooks, keys, DR and accepted risks are in [deployment/](deployment/README.md); admin routes are in [API.md](API.md).

## Restoring the platform database

The platform DB is backed up daily into restic repo `s3:<endpoint>/<BACKUP_BUCKET>/<BACKUP_PREFIX>platform/db` (app/platform_backup.py; Admin > Health > Backups). You need the backup bucket key (GitHub secret `BACKUP_*` / the `picopaas` Secret) and the repo password: the Fernet-decrypted value of `platform_settings.platform_backup_repo_password`. If the platform DB is gone, that row is gone with it, so keep the repo password somewhere outside the database (a password manager) once: read it while the platform is up with `python -c "from app import crypto; ..."` against that row.

1. Stop writers: `kubectl -n picopaas-system scale deployment/picopaas --replicas=0`.
2. From a machine with `restic` and `mariadb`: `export AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... RESTIC_REPOSITORY=s3:<endpoint>/<bucket>/<prefix>platform/db RESTIC_PASSWORD=...`; `restic snapshots` and pick one (`latest` is fine).
3. `restic dump latest db.sql > platform.sql` (the dump is stored as one plain SQL file named `db.sql`; confirm with `restic ls latest`).
   - **Alternative source: a downloaded backup.** If you downloaded the backup in Admin > Health > Backups (see ADMIN_GUIDE "Download a platform backup"), you don't need the bucket key, the repository password or `restic`: run `gunzip -c picopaas-platform-db-<date>.sql.gz > platform.sql` and continue at step 4. The file is the same `db.sql` dump the repository holds, gzipped. This path has not been drilled end to end; verify the loaded database (step 6) before you rely on it. The file holds password hashes and encrypted secrets, so delete it afterwards. The prepared copy on the platform is removed after about an hour, so download it ahead of time and keep it somewhere safe: if the platform database is gone, the Download button is gone with it.
4. Load into an empty database (create a fresh one rather than overwriting a live one): `mariadb -h <host> -P <port> -u <admin> -p --ssl-ca=ca.pem <dbname> < platform.sql`.
5. Point `DATABASE_URL` at it if it is a new database, run `alembic upgrade head` (the dump may predate the newest migration), then scale the deployment back up.
6. Check login, Admin > Health, and that tenant apps still reconcile. Backups of tenant apps are unaffected (their repos are separate).

## Ghost log noise: IMAGE_SIZE_URL

Ghost 6.72 and 6.75 (blog apps on `picopaas-example-ghost6`) log `IMAGE_SIZE_URL` with "non-permitted private IP block" on page renders when the site icon lives on Spaces. Pages still render (150 to 330 ms in Ghost's own log, 2026-11-05), so it is noise, not an outage. The cause has not been investigated. Don't confuse it with the 5 s self-fetch delay when no icon is set. Details: `examples/ghost/README.md`.

## Backups stuck or leaving leftovers

The leader sweeps every 10 minutes (`app/backup_sweeper.py`): see `sweeper` in `GET /admin/health/backups` (`last_run_at`, `cleaned`, `errors`, `orphan_repos`) and the `backup.orphan_cleaned` audit rows. A run that stays `running` past its deadline, an orphan `bk-*` Job/Secret in `picopaas-backup` or `app-*`, or a stale restic lock should clear itself within one sweep; if `errors` is not empty or `last_run_at` is old, check that the leader runs with `RECONCILER_ENABLED=1` and the backup key can list the bucket. `orphan_repos` are repo prefixes with no app config: inspect, never delete automatically. MODE=backup now runs `restic unlock` (stale locks only, never `--remove-all`) before its retention step, and a failed `forget --prune` reports one fixed reason (`repository locked`, `access denied`, `connection failed`, `repository not found`, `out of space` or `other`, never restic's own text): `retention cleanup failed: repository locked`; the platform then queues a MODE=prune follow-up. A lock from a live or recently killed Job on another host is NOT stale for 30 minutes, so that follow-up may need one more hourly retry. This needs the backup image republished and `BACKUP_IMAGE` re-pinned (bootstrap.yml with `backup_image_digest` AND `kaisen_runtime_image_digest` set to the current pins, else the runtime pin follows `:latest`). Re-pinned 2026-11-07 (bootstrap run 42373455404) to `picopaas-backup@sha256:6537bee8eae184a7598d2a3aa51fd2befa7870f13bc310405575bbbb7721a3f8`, built from 51866c1; the runtime pin was passed explicitly and stayed at `sha256:3f6ee178…`. Rollback: re-run bootstrap with the previous backup digest `sha256:bc6ebfca524f2b7a149e3c160b8b63d5b9030f1a7e75bb90eb6587fe36bfd694` and the same runtime digest.

## Kaisen runtime image and worker

- **Runtime image**: `images/kaisen-runtime/` (Dockerfile, `boot.py`, `smoke-local.sh` for a real project bundle, `ci-smoke.sh` for CI). `.github/workflows/kaisen-runtime-image.yml` builds it on changes to that folder (or by hand), smoke-tests a static bundle served from the runner (expects HTTP 200), and pushes `ghcr.io/seaburr/picopaas-kaisen-runtime:<sha>` and `:latest`. One-time: make the package Public (GitHub > Packages > picopaas-kaisen-runtime).
- **Re-pin** (needed after every republish you want live; the platform reads `KAISEN_RUNTIME_IMAGE` from `picopaas-env`, digest-pinned exactly like `BACKUP_IMAGE`): run the `bootstrap` workflow (optional input `kaisen_runtime_image_digest`, else it resolves `:latest`). Bootstrap needs Chas's approval to run. If the package does not exist yet the run warns and leaves `KAISEN_RUNTIME_IMAGE` empty (Kaisen deploys stay off). Existing preview apps keep their old image until redeployed.
- **Worker** `kaisen-worker` (`deploy/platform/kaisen-worker.yaml`, applied by `deploy.yml` with every platform image): 1 replica, `Recreate`, 50m/128Mi requests, 250m/384Mi limits, same image and `picopaas-env` as the API pods but `KAISEN_WORKER=1`, `RECONCILER_ENABLED=0`, no Service/Ingress, own ServiceAccount with no token. Egress NetworkPolicy `kaisen-worker-egress`: DNS, managed DB CIDR (3306/25060), internet 443 (private ranges excluded), Traefik pods (probes of the public URL). If a call is blocked, add the narrowest rule there.
- **Heartbeat contract**: a DB-free heartbeat thread touches `/tmp/kaisen-worker-heartbeat` every 30 s, but only while the worker's main loop has made progress within `KAISEN_HANG_SECONDS` + 120 s (see `KAISEN_OPS.md` section 2); the liveness probe restarts the pod when the file is missing or older than 120 s (first check after 90 s).
- **Rollback**: `kubectl -n picopaas-system scale deployment/kaisen-worker --replicas=0` (builds pause, API unaffected) or delete the Deployment/NetworkPolicy; revert the commit to stop `deploy.yml` re-creating it.

## Kaisen worker

`kaisen-worker` (namespace `picopaas-system`) runs Kaisen builds; image and manifest details are in "Kaisen runtime image and worker" above.

- **Heartbeat.** It touches `KAISEN_HEARTBEAT_FILE` (default `/tmp/kaisen-worker-heartbeat`) every 30 s from a side thread, but only while the main loop is making progress. Liveness restarts the pod when the file is older than 120 s. A hung run is interrupted and retried by the watchdog first; see `KAISEN_OPS.md` section 2.
- **Logs.** `kubectl -n picopaas-system logs deploy/kaisen-worker`. Lines use the platform format (logger `app.kaisen.worker`); keys and tokens are not logged.
- **Lease.** Only one worker works at a time, holding the MySQL lock `picopaas-kaisen-worker`. If a build sits in `queued`, check that the pod is Ready and holds the lease (the log says "worker started" after it does).
- **Restart.** `kubectl -n picopaas-system rollout restart deployment/kaisen-worker`. A step in flight is picked up again; a run left `running` is retried.
- **Pause.** Scale to 0 (see Rollback above). Builds stay queued.
- **Logs for the builder.** The reconciler snapshots a redacted tail of each preview's logs for `deploy_preview` (output can be one reconcile tick old); the worker itself never reads pods.


## Custom 503 page (platform down)

When the API has no ready pods, Traefik would answer with its bare "no available server". Instead the `errors` middleware swaps any 502/503/504 for a static, on-brand page (status code kept) served by `error-pages` (2 tiny busybox httpd replicas in `picopaas-system`, no ServiceAccount token, no dependency on the API or DB; ~10m CPU / 16Mi total, no new paid resource). Sources: `deploy/error-pages/{platform,app}.html` (inline CSS and logo, no external assets) and `deploy/error-pages/error-pages.yaml` (Deployment, Service, two `Middleware` CRs). `deploy.yml` and `bootstrap.yml` build the ConfigMap from the HTML files and apply the manifest BEFORE `deploy/platform/ingress.yaml`; the order matters because an Ingress that names a missing Middleware loses its router (404).

- Wired now: the `picopaas` Ingress (platform.picopaas.xyz and the apex) via annotation `traefik.ingress.kubernetes.io/router.middlewares: picopaas-system-error-pages-platform@kubernetescrd`. The `*.picopaas.xyz` catch-all 404 ingress and the platform-served "suspended" page (a 503 from the API itself) are deliberately NOT wrapped.
- Not wired yet (phase 2, tenant apps, `app.html` "This app isn't running"): needs `providers.kubernetesCRD.allowCrossNamespace: true` in `deploy/traefik/values.yaml` (helm upgrade via bootstrap) plus adding annotation `picopaas-system-error-pages-app@kubernetescrd` in `ingress_manifest()` / `custom_domain_ingress_manifest()` (app/k8s_manifests.py) and a reconcile of existing Ingresses. Side effect: an app's own 502-504 responses would also be replaced by the page. Do NOT use a Traefik entrypoint-level middleware instead: it would also replace the suspended 503 page.
- Edit a page: change the HTML, push; deploy.yml re-creates the ConfigMap, then `kubectl -n picopaas-system rollout restart deployment/error-pages` (ConfigMap mounts refresh in ~1 min without restart, but the restart is deterministic).
- Test (rollout first, then): `kubectl -n picopaas-system scale deployment/picopaas --replicas=0`; `curl -si https://platform.picopaas.xyz/ | head -20` must show `HTTP/2 503` and the PicoPaaS page, in a browser too; then `scale --replicas=2` (the next deploy also restores it). Single-replica kill: `kubectl delete pod -l app=picopaas` leaves the other replica serving. Error pages themselves: `kubectl -n picopaas-system port-forward svc/error-pages 8081:80` then open `/platform.html` and `/app.html`.
- If the platform host ever shows a 404 instead of the app: the Middleware is missing; `kubectl -n picopaas-system get middleware` and apply `deploy/error-pages/error-pages.yaml`.

## Sensitive paths

Traefik answers 404 for paths that hold secrets (`/.env*`, `/.git`, `/.aws`, `/.ssh`, `/.pgpass`, database dumps like `*.sql.gz`, `/wp-config.php`, private key names, `*.tfstate` and hundreds more) on every host, including custom domains and the platform itself, so scanners never reach an app. POST, PUT and DELETE get a 501, and they don't reach the app either.

- The rule is one Ingress with no host: `deploy/error-pages/sensitive-paths.yaml`. Its regex matches whole path segments at any depth, ignoring case. It is applied by `deploy.yml` and `bootstrap.yml`, and the requests go to the `error-pages` service.
- That file is generated: the dotfile entries of the OWASP CRS list `rules/restricted-files.data` at a pinned release, minus `skip`, plus `extra` (both in `scripts/sensitive_paths/rules.yaml`). CRS's other entries, such as `config.json` and `/package.json`, are not used because real apps serve them.
- Add or remove a path: edit `rules.yaml`, run `python scripts/sensitive_paths/build.py --write`, and update the path tables in `tests/test_sensitive_paths.py`. Only add names no real app serves: this runs in front of every app.
- Updates: the `crs-sync` workflow checks every Tuesday. On a new CRS release it regenerates the rule on a `crs-sync/<version>` branch, runs the path tests and opens an issue listing the changed entries with a link to open the PR. Review the additions, add anything real apps use to `skip`, then merge. To update by hand: `build.py --update v4.31.0`.
- Our additions (`extra`, 2026-11-08 round, from scanner probes): `.github`, `.cursor`, `.amplifyrc`, `.ebextensions`, `.elasticbeanstalk`; credentials (`credentials.json|ini`, `gcp-credentials.json`, `service-account*.json`, `secrets.json|yml|yaml`, `*.env`, `env.txt`, `appsettings*.json`); infra (`*.tf`, `samconfig.*`, `cdk.out`, `amplify/team-provider-info.json`, `Dockerfile`, `docker-compose*.yml`); logs, dumps and debug (`laravel.log`, `wp-content/debug.log`, `*.sql` and `*.sql.gz|zip|bz2`, `phpinfo.php`, `_profiler`, `actuator/env|heapdump`); AI proxy configs (`litellm*.yml`, `litellm/`, `openai-proxy*`). Deliberately not blocked: `*.php` in general, `config.json`, `settings.json`, `config.js`, `app.js`, `composer.json`. Fragments are RE2 (no lookahead) and checked against Go regexp when changed. The Ingress is one ~3.8 KB path, far under Kubernetes limits. Rollback: revert `rules.yaml`, run `build.py --write`, push (deploy.yml re-applies).
- Cost: the rule runs on every request, about 16 microseconds per path measured on 2026-11-05 (Apple silicon, Go regexp).
- An app that really serves such paths turns on **Serve sensitive paths** in its settings (`allow_sensitive_paths`, spec `allowSensitivePaths`). Its Ingress then gets a higher `router.priority`, so its own route wins on its hosts. The Forgejo template does this.
- Test: `curl -si https://<any-app-host>/.env` answers 404 with the plain busybox page. If `error-pages` is down, these paths answer 503 and still don't reach the app.
- Limit: this stops known paths only. A secret at a path not on the list is still served. Kaisen's static runtime has its own broader guard (docs/design/kaisen.md 16.10).

## Reading platform logs

Every platform line carries a UTC timestamp, level, logger, thread and the call site (`app/logging_setup.py`):

```
2026-10-29T14:03:12.345Z WARNING app.reconciler [ThreadPoolExecutor-0_0] reconciler.py:123 reconcile_app() app 7 failed
2026-10-29T14:03:12.400Z INFO    access 10.150.0.57:51832 "GET /healthz HTTP/1.1" 200
```

Set `LOG_LEVEL=DEBUG` on the platform deployment for more detail (default `INFO`). `apscheduler`, `httpx` and `httpcore` stay at
`WARNING` regardless, because they log every 15 s job run and every request URL. Admin > Health > Logs still redacts secrets
before showing or downloading lines.

## CI lint

The `lint` CI job runs `ruff check .` (ruff pinned in `requirements-dev.txt`, config `ruff.toml`: defaults E4/E7/E9/F, no E501, F811 off in `tests/`; no `ruff format`), `actionlint` on `.github/workflows/*.yml` (release pinned in the job) and `shellcheck` on every tracked `*.sh` plus `.githooks/pre-commit`. Intentional exceptions carry a `# shellcheck disable=` or `# noqa` line with a reason. Run locally: `ruff check .`.

## CI frontend lint

The `frontend` CI job runs `npm run lint` (ESLint 9 flat config: eslint-plugin-vue, typescript-eslint, vuejs-accessibility; no formatting rules; lints `src`, `e2e`, `scripts`). Zero warnings allowed; inline `eslint-disable` needs a reason comment.

## CI coverage floor

Three CI jobs run live-server tests that `backend-tests` skips: `mysql-integration`, `postgres-integration` (PostgreSQL 16 and 17, TLS only, non-superuser admin; `tests/test_postgres_admin.py`) and `garage-integration` (the ObjectStore contract and generic backups; a skipped test fails the job). Run them locally with `scripts/dev/postgres-up.sh` or `scripts/dev/garage-up.sh`, then source the env file each one prints. Spaces has no CI job.

The `backend-tests` CI job runs pytest with `--cov=app` and fails below `fail_under` in `.coveragerc` (93%; measured 95.25% on 2026-11-02, rule: measured total rounded down, minus 2). To raise it, run `python -m pytest tests/ -q --cov=app --cov-report=term`, apply the same rule to the new total and edit `.coveragerc`. The `mysql-integration` subset runs without `--cov`, so the floor does not apply to it.

## Kaisen image unit (kaisen-imgproc)
Secret-less pod (`deploy/platform/kaisen-imgproc.yaml`, same image, `python -m app.imgproc`) that decodes/re-encodes uploaded images. Ingress only from `app=picopaas` on 8080, no egress. `picopaas-system` has no namespace default-deny and the API pod has no egress policy, so no extra rule is needed for API -> `kaisen-imgproc:8080`.
- deploy.yml applies it LAST and non-fatally: a failure shows as a `kaisen-imgproc` warning annotation, the platform deploy stays green, uploads answer 503.
- Check: `kubectl -n picopaas-system get deploy,pod,networkpolicy -l app=kaisen-imgproc` and `kubectl -n picopaas-system logs deploy/kaisen-imgproc`.
- Before enabling uploads: measure peak memory (`kubectl top pod`) with a ~8 MP image; verify `SELECT @@max_allowed_packet` on the managed MySQL is >= 16777216 (`doctl databases configuration get <db-id> --engine mysql`, or the DO console, Settings > Configuration).
- Rollback: `kubectl -n picopaas-system delete deploy,svc,networkpolicy,sa kaisen-imgproc` (uploads flag stays off; nothing else depends on it).
