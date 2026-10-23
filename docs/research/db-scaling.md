# Platform database scaling review

Date: 2026-10-23. Scope: the platform's own database (MySQL in prod, SQLite in tests), not tenant databases.
Method: static read of `app/models.py`, all 30 alembic revisions, every query in `app/routers/`, `app/reconciler.py`,
`app/scheduler.py`, `app/health.py`, `app/quota.py`, `app/image_check.py`, `app/image_history.py`, `app/ratelimit.py`,
`app/dependencies.py`, `app/main.py`, `app/db.py`. No code was changed. Two micro-benchmarks were run (see "Measured vs estimated").

Context that bounds some findings: `MAX_APPS_PER_USER=5`, so per-user list endpoints are small regardless of total
users. Registration is approval-gated, but pending users still occupy rows. Prod runs 2 platform replicas
(`deploy/platform/deployment.yaml`), one of which is the reconciler leader.

Severity key: NOW = already costly or a correctness risk; 10k = breaks at ~10k apps/users; 100k = breaks at ~100k rows.
"Apps" below means live app rows; the reconciler cost is per live running app.

## Summary table

| # | Finding | Where | Breaks at | Fix | Effort |
|---|---------|-------|-----------|-----|--------|
| 1 | Reconcile tick does ~6 lazy-load queries + manifest render + Fernet decrypt per running app, every 15s | `reconciler.py:673-740`, `models.py:152-169` | ~500 apps tick > 15s (est.); 10k = 60k queries/tick | Eager-load (selectinload), skip unchanged apps via cheap hash inputs, shard/batch | M |
| 2 | `set_user_connection_limit` opens a new MySQL connection + `ALTER USER` per DB app per sweep (every 15s) | `reconciler.py:385-392`, `providers/mysql.py:132-148` | ~100-200 DB apps (connection churn on DB host, TLS handshakes) | Run once per app (store applied value) or on a slow cadence/at limit change | S |
| 3 | Metrics sampling: 3-4 statements per app per minute inside one transaction; `OFFSET 180` prune | `health.py:206-239` | ~2k apps (est. 6k+ round trips/min over network); 10k bad | Bulk insert; one prune per cycle by `ts` cutoff; index `(app_id, ts)` | M |
| 4 | Audit log never pruned; unbounded growth; export loads 50k ORM rows + pydantic + full body in memory | `models.py:303`, `routers/admin.py:489-515` | 100k rows (table), export ~150 MB RSS per request (measured) | Retention job in batches, stream export (`yield_per` + `StreamingResponse`), keyset pagination | M |
| 5 | Pool: SQLAlchemy defaults (5 + 10 overflow) x 2 replicas vs sync threadpool of 40; no explicit sizing against managed MySQL `max_connections` | `app/db.py:10-15` | Under load now-ish: `QueuePool limit` 30s timeouts | Set `pool_size`/`max_overflow`/`pool_timeout` explicitly; size to DB limit | S |
| 6 | Admin lists (`/admin/users`, `/admin/apps`) unpaginated with N+1 (`app_count` per user, `a.owner` per app) | `admin.py:119-150, 281-300` | 1k users (1k extra queries/page); 10k broken | Pagination + one grouped COUNT / `joinedload(owner)` | S-M |
| 7 | Admin health `build()` loads all live apps + joins users on every refresh and returns every app and pod in one JSON | `health.py:159-190` | 5k apps (MB-scale payload every poll) | Aggregate server-side; paginate per-app table; cache DB part | M |
| 8 | Missing/weak indexes for hot filters: `apps(deleted_at,status)`, `apps(owner_id,deleted_at)`, `audit_log(actor_id)`, `app_metric_samples(app_id,ts)` | `models.py` (see below) | 10k apps (scans of `apps` 4x per tick) | Add composite indexes (DDL below) | S |
| 9 | Unauthenticated hostname probe hits DB per unique Host (10s cache cleared at 1000 entries) | `main.py:153-178` | Anyone can force ~1 query per request with random hosts | Negative-cache by pattern, cap/LRU, or check against in-memory app-host set | S |
| 10 | Reconcile sweep holds one open read transaction over all k8s/DB-admin calls; first slow app delays all others | `reconciler.py:673-740` | Any size; worse with 1 | Commit/rollback per app; per-app timeouts; bounded worker pool | M |
| 11 | Leader check only at job start; two jobs can overlap after a lock loss | `scheduler.py:34-57` | Failover edge case | Re-check leadership between apps; fencing via DB row/TTL | S-M |
| 12 | Soft-deleted apps/users never purged; `apps` grows monotonically | `models.py`, `reconciler.py:745-760` | 100k | Purge job after N days (rows already torn down) | S |
| 13 | `audit.record` commits on the request session (extra round trip, expires all loaded ORM state) | `audit.py:30-32` | Latency, not a break | Write via separate short session or batch on commit | S |
| 14 | `AppMetricSample.per_pod` JSON text parsed in Python on every history read | `app_ops.py:159-192` | Small (180 rows max per call) | Fine now; drop `per_pod` after N hours if size matters | S |

Things that are fine (checked): API key lookup is a unique-index point query; `last_used_at` writes are throttled to
once per 5 minutes per key (`dependencies.py:20,67-75`); session cookie auth is one PK `get`; `app_events` and
`app_image_history` are pruned per app (20 and 5 rows); `auth_throttle` is pruned probabilistically (2%) on a
`window_start` index; `choose_server` is one GROUP BY over `app_databases` (index on `db_server_id` exists in MySQL via the FK).

## MySQL vs SQLite note on indexes

InnoDB automatically creates an index for any FOREIGN KEY column that lacks one, so on MySQL these are already indexed
even though no migration names them: `apps.owner_id`, `api_keys.user_id`, `app_env_vars.app_id` (also leftmost of the
unique `(app_id,key)`), `app_databases.db_server_id`. In SQLite (tests) they are not. This hides nothing in prod but
means test timings don't reflect index use. Explicitly declaring them (`index=True`) is a cheap hygiene fix and makes the
schema autogenerate-stable. InnoDB secondary indexes append the PK, so `ix_app_metric_samples_app_id` already serves
`WHERE app_id=? ORDER BY id DESC` as a covering range scan (this is why the id-based prune is cheap per statement).

Existing explicit indexes: `users.email` (unique), `api_keys.hashed` (unique), `apps.hostname` (unique),
`apps.platform_subdomain` / `apps.custom_domain` (unique), `db_servers.name`, `audit_log.at`,
`auth_throttle.window_start`, `app_events.app_id`, `app_image_history.app_id`, `app_metric_samples.app_id`.

## Details

### 1. Reconcile tick is O(apps) with several queries each (NOW design risk, 10k breaks)
`reconcile_all` (`reconciler.py:~665-770`) runs every 15s (`scheduler.py:321`, `max_instances=1, coalesce=True`):
- `pending_or_deploying`, `errored`, `running` and `not_cleaned_up` are four separate `SELECT * FROM apps` scans
  filtered on `deleted_at IS NULL`, `status`, `error_retryable`. No index on `status` or `deleted_at`, so each is a full
  table scan (fine at 100 rows, 4 scans x 10k rows x 15s at 10k; also pulls every wide column incl. text).
- For every running app, `reconcile_app(sweep=True)` touches the lazy relationships `app.database` (+`.server`),
  `app.storage`, `app.env_vars` (`reconciler.py:~385-440`), then `image_history.record_running` lazily loads
  `app.image_history` (`image_history.py:20`), and `record_app_events` runs one `SELECT dedupe_key, at FROM app_events
  WHERE app_id=?` per app that has pods (`reconciler.py:197`), plus `app.events` for `last_event` in list responses.
  That is roughly 5-6 queries per app per tick (selectin/lazy default is "select"), i.e. ~6 x apps queries per 15s.
- It then decrypts every env var with Fernet, renders the full manifest set, JSON-dumps and HMACs it, just to compare
  `rendered_hash` (`reconciler.py:~468-474`). CPU-bound in the leader pod, per app, every tick. At ~5-10 ms per app
  (estimate) this exceeds the 15s tick at ~1.5k-3k apps; coalescing then silently skips ticks, so newly created apps wait.
- The only batched k8s calls are `list_app_namespaces` and `app_pod_events` (one each per tick). Good. But anything
  that falls out of the hash-match path (e.g. platform upgrade changing every manifest) causes N apply calls in a row
  with `get_used_limits`, `apply`, `delete_ingress`, `delete_network_policy`, `get_status` (about 5 serial k8s calls
  per app, seconds each): a platform upgrade at 1k apps is a multi-hour serial re-apply, and new user apps queue behind it.

Fix:
- Query-side (S): `options(selectinload(App.database).selectinload(AppDatabase.server), selectinload(App.storage),
  selectinload(App.env_vars), selectinload(App.image_history))` on the running query (4 extra queries per tick total,
  not per app). Replace per-app `app_events` query in `record_app_events` with one `SELECT app_id, dedupe_key, at WHERE app_id IN (...)`.
- Indexes (S): see #8.
- CPU-side (M): store an input fingerprint (`updated_at`/`spec_version` bumped on every user/admin/spec change, plus a
  platform manifest version constant). Sweep skips render entirely when both unchanged; do a full render only on
  platform-version change, spread over time (e.g. `LIMIT 50` stale apps per tick, oldest `last_reconciled_at` first).
- Throughput (M): bound parallelism with a small worker pool (4-8) for pending/deploying/stale apps; pending apps must
  never wait behind a drift re-apply (separate passes already exist, but drift is serial after them in the same tick).

### 2. Per-app MySQL admin connection every sweep (NOW)
`reconciler.py:385` ("Self-heal: re-apply the per-user connection cap") runs for every running app with a DB on every
15s tick: `mysql_admin_factory(server).set_user_connection_limit` opens a new pymysql connection (TLS to managed MySQL)
and runs `ALTER USER`. At 50 DB apps that is 200 TLS connects/min against the DB host (and `ALTER USER` takes metadata
locks and writes the grant tables); at 1k DB apps it is 4k connects/min and the managed DB's `max_connections` and CPU
become the bottleneck. In procedure mode (`MYSQL_ADMIN_USE_PROCEDURES=1`) it is a no-op, so prod may not feel it today;
verify which mode prod uses. Same pattern applies to the Postgres provider.
Fix (S): add `app_databases.applied_max_connections` (nullable int); only call when it differs from
`effective(app)["db_max_connections"]`. Optionally reuse one connection per server per tick.

### 3. Metrics sampling (`health.py:206-239`) (10k)
Per live non-suspended app per 60s: INSERT + flush, `SELECT id ... ORDER BY id DESC OFFSET 180 LIMIT 1`, and a DELETE,
all in a single long transaction committed at the end, iterating `db.query(App).all()` (ORM objects, includes apps with no
pods which `continue` but only after loading). Steady state is capped at 180 rows/app (1.8M rows at 10k apps, 18M at 100k,
~60-70 bytes of data + index overhead plus the `per_pod` text, so ~0.3-0.5 GB at 10k apps; 3-5 GB at 100k).
`OFFSET 180` is index-range cheap per app, but it is 3 round trips per app: at ~1 ms in-cluster latency that is ~30 s for
10k apps per minute, plus a transaction holding row locks for that long. The DELETE `id <= cutoff` is efficient
(range on the app_id index). Row churn: every app rewrites its rows constantly, good for InnoDB purge but noisy for binlog/backups.
Measured (SQLite in-memory, no network): 1,000 apps = 0.28 s, 5,000 apps = 1.25 s per tick with 180 rows/app preloaded.
Linear and fine locally; the real cost on MySQL is round trips, so treat the SQLite figures as a lower bound.
Fix (M):
- Single `INSERT ... VALUES (...), (...)` (`session.execute(insert(Model), rows)` batches via executemany) for all apps.
- Prune once per cycle, by time: `DELETE FROM app_metric_samples WHERE ts < :cutoff LIMIT 5000` repeated until 0 rows, with
  index `ix_app_metric_samples_app_ts (app_id, ts)` (the app-scoped read `ORDER BY ts DESC LIMIT 180` uses it too).
  Time-based retention (3 hours = 180 points at 60 s) makes this correct and removes the OFFSET query.
- Query only apps present in the snapshot (`WHERE id IN (...)` from the namespaces with pods) instead of all apps.
- At 100k, consider RANGE partitioning by day or moving samples out of MySQL (the Health graph needs only recent points).

### 4. Audit log growth, export and listing (100k, memory now)
- `audit_log` has no retention anywhere. Every login, register, app create/update/delete, admin action, image-watch
  auto-update (`image_check.py` writes audit rows "system") appends a row (39 `audit.record` call sites). Rows are narrow (~250 bytes max),
  so 1M rows is about 300-400 MB with the `at` index. It is not urgent but is the only unbounded-forever table
  (soft-deleted apps/users are the other, #12). Image watch on N apps with `auto_update` can write N rows per update cycle.
- `GET /admin/audit?limit<=1000` (`admin.py:519`) is `ORDER BY id DESC LIMIT n` - O(limit), fine at any size. But there is
  no `offset`/cursor and no filter, so older rows are only reachable via export.
- `GET /admin/audit/export` (`admin.py:496-515`) loads up to `AUDIT_EXPORT_MAX = 50_000` ORM rows, validates each into a
  pydantic model, builds the entire JSON/CSV string and then the Response body: three full copies in memory.
  Measured on 100k seeded rows (SQLite): 1.0 s CPU, peak 154 MB traced Python allocations, 10 MB body for 50k JSON rows.
  With 2 replicas at a 384 MB-class limit (check the platform pod limit), two concurrent exports or one export plus
  load can OOM the pod, which is also the reconciler leader. The `audit.export` row itself is written after building.
Fix (M):
- Retention (S): scheduled leader job `DELETE FROM audit_log WHERE at < :cutoff ORDER BY id LIMIT 5000` in a loop
  (the `at` index exists); default 365 days (config `AUDIT_RETENTION_DAYS`), documented; export is the archive path.
- Export (S): `db.query(...).yield_per(1000)` into a `StreamingResponse` generator writing CSV/NDJSON line by line;
  drop the pydantic copy (map rows to tuples). Keep the 50k cap or replace with `since`/`until` params.
- Listing (S): keyset pagination `GET /admin/audit?before_id=...&limit=...` (+ optional `action`, `actor_id`) and
  indexes `audit_log(action, id)` / `audit_log(actor_id, id)` only if filters are added.

### 5. Connection pool vs managed MySQL limits (NOW)
`app/db.py:10-15` sets `pool_pre_ping` and `pool_recycle=1800` but not `pool_size`/`max_overflow`/`pool_timeout`, so
SQLAlchemy defaults apply: 5 persistent + 10 overflow = 15 connections per process, 30 s `pool_timeout`.
Per replica, FastAPI runs sync endpoints on AnyIO's default 40-thread pool, so up to 40 concurrent requests can
contend for 15 connections; the 16th waits up to 30 s, then raises `TimeoutError: QueuePool limit ... reached`
(this surfaces as 500s under a modest burst, e.g. the Health page polling plus a CLI loop). Total demand today is
2 replicas x 15 = 30, plus one pinned connection for `GET_LOCK` (`scheduler.py:47`, held for the process lifetime,
on every replica - the follower keeps its connection too only if it ever tried; it does, every 15s), plus the
scheduler jobs' own sessions (reconcile, quota, image watch, metrics, health refresh) each taking a pool connection and,
for reconcile, holding it for the whole tick. Managed DO MySQL has a small per-plan `max_connections`
(low tens to ~100 on the smallest plans; verify with `SHOW VARIABLES LIKE 'max_connections'` on prod). Tenant apps with
a DB use the same server only if `primary` hosts both platform and tenant DBs; if so tenant connections compete for the
same limit (each tenant user capped at `MAX_DB_USER_CONNECTIONS=10`, so 6 DB apps can exhaust 60).
Fix (S): in `db.py` set `pool_size=8, max_overflow=4, pool_timeout=10` (tune to `max_connections / replicas - headroom`),
expose via env (`DB_POOL_SIZE`, `DB_MAX_OVERFLOW`), reserve scheduler sessions, and add a `/admin/health` line showing
pool `checkedout()`. Also keep platform data on a separate DbServer from tenants (it already is a distinct role in `DbServer`).
Consider `expire_on_commit=False` for the scheduler session factory to avoid reload storms (#13).

### 6. Admin lists: unpaginated, N+1 (10k)
- `GET /admin/users` (`admin.py:146-152`): `SELECT * FROM users WHERE status != 'deleted' ORDER BY id`, then `_user_out`
  runs one `SELECT COUNT(*) FROM apps WHERE owner_id=? AND deleted_at IS NULL` per user. 10k users = 10k+1 queries and a
  multi-MB response per page load. The UI also renders all of them.
- `GET /admin/apps` (`admin.py:297-300`): all live apps, `_app_admin_out` touches `a.owner.email` (lazy; one query per
  distinct owner), up to 5 apps per owner - so ~apps/3 extra queries, and `resources.describe(a)` computed per app.
- `GET /admin/db-servers` and `/admin/health`: `_server_out` runs a COUNT per server (tiny N, fine).
- User-facing `GET /apps` (`apps.py:212`) returns all of one user's apps (capped at 5 by policy), but `_app_out` touches
  `app.events` (full list, up to 20), `app.env_vars`, `app.database`, `app.storage`: up to ~4 lazy loads x 5 apps = 20 queries.
  Add `selectinload` for those (S); pagination is unnecessary while the per-user cap holds.
- Pending queue: `health.py:257` (pending users, `LIMIT 20`) is bounded. `users.status` has no index, so the
  `GROUP BY status` counts (`health.py:252, 261`) scan the table on every Health refresh (and `apps.status` likewise).
Fix: add `limit`/`offset` (or keyset on `id`) with `{items, total}` envelope plus `q=` search on email; replace per-user
count with `SELECT owner_id, COUNT(*) ... WHERE owner_id IN (:page) GROUP BY owner_id`; `joinedload(App.owner)`.
For compat, keep the old shape behind a default `limit=100` and let the UI page.

### 7. Admin health snapshot (10k)
`health.build()` (`health.py:159-190`) runs on each admin Health request: joins every live app to its owner, computes
per-app aggregates and returns every app with its pods. Plus three `COUNT(*)` queries for engine/db/storage counts and two
`GROUP BY`s (`health.py:252-275`) - each a full scan today. At 5k+ apps the JSON is megabytes and CPU is spent building it
on the request thread. The k8s snapshot is cached 15 s, but the DB-derived part is rebuilt per request.
Fix (M): cache the DB-derived portion for ~15 s alongside the snapshot (one dict shared across admin requests); return
summary + top-N unhealthy apps by default and page the full table; collapse the five counting queries into one
`SELECT status, want_db, want_storage, db_engine, COUNT(*) ... GROUP BY ...` (S).

### 8. Index DDL
All on MySQL; add as one Alembic revision (`op.create_index`). Sizes assume InnoDB; creation is online (`ALGORITHM=INPLACE`).
```sql
-- reconciler: four scans of apps per tick; health counts; is_suspended_host
CREATE INDEX ix_apps_deleted_status      ON apps (deleted_at, status);
-- user-facing list + per-user COUNT, admin _user_out (replaces the implicit FK index on owner_id)
CREATE INDEX ix_apps_owner_deleted       ON apps (owner_id, deleted_at);
-- metrics: time-based prune and recent-points read
CREATE INDEX ix_app_metric_samples_app_ts ON app_metric_samples (app_id, ts);
-- (drop the single-column ix_app_metric_samples_app_id afterwards; the composite covers it)
-- events: ORDER BY at DESC, id DESC OFFSET 20 per app
CREATE INDEX ix_app_events_app_at        ON app_events (app_id, at, id);
-- users: status counts, pending queue (ORDER BY created_at, id), admin list
CREATE INDEX ix_users_status_created     ON users (status, created_at);
-- only if audit filtering is added
CREATE INDEX ix_audit_log_actor_id       ON audit_log (actor_id, id);
CREATE INDEX ix_audit_log_action_id      ON audit_log (action, id);
```
Notes: `apps.status` has low cardinality (5 values) so `(deleted_at, status)` mostly helps by excluding the (growing)
soft-deleted tail; the `NULL`-first `deleted_at` key makes the live set a contiguous range. If deleted rows are purged
(#12) the benefit shrinks, which is fine. Declare the FK indexes explicitly in `models.py` (`index=True`) for
`apps.owner_id`, `api_keys.user_id`, `app_env_vars.app_id`, `app_databases.db_server_id` so SQLite tests and
autogenerate match MySQL. `apps.created_at` ordering for `list_apps` is per-owner (<=5 rows) so no index needed.

### 9. Suspended-host probe (`main.py:153-178`)
`_is_suspended_host` is hit by the wildcard catch-all for unknown hosts. A 10 s positive/negative cache per host
exists, but it is keyed by the attacker-controlled Host header and cleared wholesale at 1,000 entries. A scanner sending
random subdomains defeats the cache and costs one indexed query (two unique indexes, `OR`) plus a pooled connection
per request, competing with real traffic for the small pool (#5). Fix (S): rate-limit this path (`ratelimit.py`) or
bound by a strictly-LRU cache with a minimum hit window; negative results for labels that don't match the
`adjective-animal-xxxxxxx` shape can be answered without touching the DB.

### 10. Transactions across network calls (10k; correctness risk now)
`reconcile_all` loads `running` apps then loops. For the common no-op sweep path `reconcile_app` returns without a
commit, so one REPEATABLE READ transaction (started at the first SELECT) stays open across `list_app_namespaces`, every
`set_user_connection_limit` (network), `kube.apply` (seconds each) and event recording. Effects: stale snapshot (an
admin's suspend or a user's spec change committed on another replica after the snapshot started is invisible until the
next tick, and a sweep `session.commit()` may write back stale in-memory columns); a long-lived read view prevents purge
of old row versions (history list growth, mostly harmless at this size); and the connection is pinned the whole time.
Also, a single hung app (k8s apply timeout) blocks every subsequent app in the tick.
Fix (M): `session.rollback()`/`commit()` after each app (or `expire_all()` + refetch by id as `image_watch` does,
`image_check.py:157-170`), per-call timeouts on the kube client, and iterate ids rather than a materialized ORM list.

### 11. GET_LOCK leadership (edge cases)
`try_acquire_leader` (`scheduler.py:34-57`) pins one connection per process and calls `GET_LOCK(name, 0)` at the start of
each job. Strengths: lock auto-releases if the connection dies; reentrant on the same connection so the leader keeps
it; guarded by a Python lock against concurrent use. Gaps:
- The check is at job start only. If the leader's lock connection drops mid-tick (DB failover, `wait_timeout`), the
  other replica becomes leader within 15 s while the old leader's tick continues - two reconcilers briefly overlap.
  The reconciler is idempotent so impact is duplicate k8s applies/DB provisioning races, but `create_db_user` for the same
  app on both is the risky one. Fix (S): check leadership again before each provisioning step, or add a fencing token row.
- Each job on each replica calls the lock path: the follower issues `SELECT GET_LOCK` plus `commit` every 15 s (plus the
  other five jobs), ~10 trivial queries/min. Negligible.
- Not safe behind a transaction-mode pooler (ProxySQL, DO connection pools) because the lock is session-scoped. DO managed
  MySQL has no pooler, so OK today; note in ops docs if a pooler is added.

### 12. Soft-delete tail and growth of `apps`
Apps and users are soft-deleted (`deleted_at`/`status='deleted'`) and never purged (`release_unique_hosts` frees hosts but
the row stays). `apps` rows are wide (~60 columns, several 255-char strings). At 100k deleted apps, scans in #1 and #6
include them unless indexed (#8). `not_cleaned_up` (`deleted_at IS NOT NULL AND status != 'deleted'`) is a scan every tick.
Fix (S): leader job to hard-delete apps where `status='deleted' AND deleted_at < now()-30d` (children cascade; check
the FKs without `ondelete` on `app_env_vars`, `app_databases`, `app_storage` - the ORM cascades via
`cascade="all, delete-orphan"`, but raw DELETEs would fail), batch 500 per run. Also purge `deleted` users with no apps and
revoked `api_keys` after N days.

### 13. Audit writes on the request path
`audit.record` calls `db.commit()` on the request's session (`audit.py:30-32`). With `expire_on_commit=True` (the
default, `db.py:16` doesn't override it) all loaded objects are expired and re-selected on next access, e.g. `create_app`
calls `db.refresh(app)` after commit, then `audit.record` commits again, then `_app_out(app)` reloads `app` plus its
relationships. That is ~2 redundant round trips per mutating request; not a scaling cliff, but each costs a pooled
connection time slice. Fix (S): `SessionLocal = sessionmaker(..., expire_on_commit=False)` after a quick test run, or
write audit rows from a short second session so the main object graph isn't invalidated.

### 14. Text/JSON parsing in Python
Only `AppMetricSample.per_pod` is JSON-in-text; parsed per row on `GET /apps/{id}/metrics-history` (<= 180 rows, tiny).
`AppEnvVar` is Fernet-decrypted per var in Python every reconcile (see #1) and on spec GET; env var counts per app are
small. Nothing needs SQL JSON support. If row size matters at 100k apps, stop persisting `per_pod` once it ages out
of the "current pods" view (it is only needed for the latest few points) or store it in a side table.

## Measured vs estimated
- MEASURED (local SQLite in-memory, Python 3, no network, `-I` mode; script kept outside the repo in the session scratchpad):
  audit export of 50k rows out of 100k: 1.02 s, peak 154 MB traced allocations, 10.1 MB JSON; metrics sampling loop
  (INSERT + OFFSET query + DELETE per app, 180 preloaded rows/app): 0.28 s at 1,000 apps, 1.25 s at 5,000 apps.
  These show Python/ORM cost scales linearly; they do NOT show MySQL network latency, lock contention or buffer-pool behaviour.
- ESTIMATED (reasoning from the code and typical latency; not measured): reconcile CPU per app (5-10 ms render+decrypt+hash),
  per-app query counts (counted from lazy relationships), `ALTER USER` connection cost, pool exhaustion thresholds, MySQL
  `max_connections` of the managed plan (verify), table sizes (row-width arithmetic).

## Prioritized fix list

**Status (2026-10-23):** P1 items 1-4 are DONE (pool env knobs and sizing, applied connection limit recorded in
`app_databases.db_conn_limit_applied`, eager loading plus batched `app_events` lookup with a query-count test, indexes
migration `a7c1e9b3d5f2`). Item 5 (audit retention / streamed export / keyset pagination) is ON HOLD. Note: the
`ix_app_events_app_id` and `ix_app_metric_samples_app_id` indexes were replaced by the composite ones.

P1 (do soon, all S unless noted; each independent):
1. Pool sizing and env knobs in `app/db.py` (#5) - S. Prevents burst 500s; cheap.
2. Record applied connection limit, stop `ALTER USER` per sweep (#2) - S.
3. Eager-load relationships on the reconciler's running query and `list_apps`; batch the `app_events` lookup (#1 query part, #6) - S.
4. Add indexes migration (#8) and `index=True` on FK columns - S.
5. Audit retention job + streamed export + keyset-paginated listing (#4) - M.

P2 (before ~1k apps):
6. Batch metrics sampling and time-based prune; `(app_id, ts)` index; sample only apps with pods (#3) - M.
7. Per-app commit/rollback and kube timeouts in the sweep; bounded worker pool; fingerprint skip to avoid re-rendering (#1, #10) - M/L.
8. Paginate `/admin/users` and `/admin/apps`; grouped counts; cache DB part of admin health (#6, #7) - M.

P3 (before ~10k apps / 100k rows):
9. Purge job for soft-deleted apps/users/revoked keys (#12) - S.
10. Leadership re-check between provisioning steps / fencing (#11) - S-M.
11. Rate limit or bound the suspended-host probe (#9) - S.
12. `expire_on_commit=False` or separate audit session (#13) - S.
13. At 100k+: partition or externalize metric samples; consider a read replica for admin/health reads - L.

Effort key: S under half a day, M about a day, L multiple days.
