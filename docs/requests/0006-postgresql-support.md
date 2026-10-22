# 0006: Managed PostgreSQL support (status: PROPOSED, research + design only; NOT approved, nothing built)

Chas asked for a roadmap and design for PostgreSQL alongside (or eventually instead of) MySQL, and was explicit: do not
build anything. This file is the feasibility study and the budget request. No code, migration, provider or Terraform
exists for it. Nothing here may be provisioned until Chas marks it APPROVED.

1) **What is being deployed.** (Proposed, not yet.) A second database engine: a `PostgresAdmin` provider that
   implements the same per-app isolation model as MySQL, engine-awareness in `DbServer`/placement/env refs/egress
   policy/quota, and a Postgres server to host the databases. Recommended first step: an **in-cluster** single-instance
   Postgres (StatefulSet in `picopaas-data`, 1 small PVC, nightly dump to the existing Spaces bucket) as a pilot;
   promote to **DO Managed PostgreSQL** (basic `db-s-1vcpu-1gb`, single node, NYC3, trusted sources = the cluster) only
   if the pilot proves demand.
2) **Why.** Postgres unlocks a large class of popular self-hosted apps that are Postgres-only or Postgres-first
   (n8n, Listmonk, Miniflux, Outline, ...), and removes the `sql_require_primary_key` clash that rejected Wiki.js.
3) **Cost.**
   - Pilot, in-cluster: **~$0-2/mo static** (a PVC of ~10 GiB at about $1/mo; RAM/CPU of roughly 256-512 MB / 100-250m
     taken from the existing node, which shrinks free capacity for tenant pods and may bring the autoscaled second
     node (+~$28) up sooner). **$0 per app** (it is another database on the same server, like MySQL today).
   - Managed: **~$15/mo static** (the same `db-s-1vcpu-1gb` single-node tier as our MySQL, $15.15 per
     `docs/requests/0001-core-infra.md`; confirm the current DO price list before approving). **$0 per app** up to the
     server's connection and disk limits (see "Capacity" below). This pushes the baseline from about $60 to about $75/mo
     (about $103 with the second node up), over the approximate $60/mo cap in CLAUDE.md, so it needs Chas's explicit
     approval.
   - Switching entirely to Postgres (retiring MySQL) would be cost-neutral at steady state (one managed DB instead of
     two) but is a large migration; see section 5 of the design.

Approval line (Chas fills in): `APPROVED: <variant> on <date>` / `DECLINED`.

---

# Design: can Postgres support our isolation model?

**Short answer: yes for every protection except the hard size quota, which becomes a two-stage, weaker-by-default
design.** Everything else maps one-to-one with equal or better strength. Details below.

## 1. Inventory: protections enforced today (MySQL)

| # | Protection | Where | How it works today |
|---|---|---|---|
| 1 | Per-app isolation | `app/providers/mysql.py::create_db_user`, `_db_name`/`_db_user` | One database `app_<id>` plus one user `app_<id>'@'%'` with a random 32-char password; `GRANT ALL ON app_<id>.*` only. Identifiers regex-validated (`_IDENT_RE`) because MySQL cannot bind identifiers. |
| 2 | Provider abstraction | `app/providers/base.py::MySQLAdmin` (`create_db_user`, `drop`, `set_user_connection_limit`, `server_health`, `db_size_bytes`, `set_read_only`, `set_password`), fake in `app/providers/fake.py` | Real impl is constructed per `DbServer`; callers never pass host/creds per call. |
| 3 | Placement | `app/db_placement.py::choose_server`, `app/models.py::DbServer` (`host`, `port`, `admin_user`, `admin_password_encrypted`, `max_databases`, `status`, `ca_cert`) | Picks the `active` server with most free slots (`max_databases` minus `AppDatabase` count). No engine concept: every server is MySQL. |
| 4 | Bootstrap | `BOOTSTRAP_DB_HOST/_PORT/_USER/_PASSWORD` (CLAUDE.md) | If all set and no `DbServer` exists, insert one named `primary`. |
| 5 | TLS | `DbServer.ca_cert`, `app/env_refs.py` (`db.ca_cert`, `db.tls`), `app/db_tls.py::mysql_ssl_context`, USER_GUIDE section 6 | Managed MySQL requires TLS. The CA PEM is exposed to the app as `${{ db.ca_cert }}`; `${{ db.tls }}` is `required` if a CA exists, else `off`. |
| 6 | Network egress | `app/k8s_manifests.py::network_policies`, policy `allow-egress-mysql`, `MYSQL_PORT = 3306`, `mysql_cidr`/`mysql_port` params; `MYSQL_EGRESS_MODE` in-cluster (ns `picopaas-data`, pod `app=mysql`) or cidr | A tenant pod may reach only its DB server host:port. In-cluster uses a podSelector peer; managed uses an `ipBlock`. |
| 7 | Connection cap | `config.MAX_DB_USER_CONNECTIONS` (10); `create_db_user` (`WITH MAX_USER_CONNECTIONS`), `set_user_connection_limit` | Per-user cap, re-applied idempotently so raised/lowered config reaches existing users. |
| 8 | Size quota | `app/quota.py::_poll_db`, `config.DB_QUOTA_BYTES` (50 MB), `DB_QUOTA_POLL_SECONDS` (60), `MySQLAdmin.db_size_bytes` (sum of `data_length + index_length`, stats expiry forced to 0), `set_read_only` | Polled. Over cap: `REVOKE INSERT, UPDATE, CREATE, ALTER`; SELECT/DELETE/DROP stay so the user can clean up. Restored with GRANT when back under. Because MySQL applies DB-level REVOKE to an open session only on its next `USE`, the provider also **kills the app user's sessions** (as the app user first, since managed `doadmin` cannot kill other users' threads, else admin/procedure path) and the reconciler **rolls the pod** if kills cannot be verified. |
| 9 | Credential rotation | `MySQLAdmin.set_password` | Rotates the app user's password; app secret/env updated. |
| 10 | Server constraints | DO default `sql_require_primary_key=ON` | Rejects tables without a PK (this is why Wiki.js was rejected, see RECIPES "apps we evaluated"). |
| 11 | Restricted admin mode | `config.MYSQL_ADMIN_USE_PROCEDURES` (`picopaas_ops.set_app_read_only`, `app_size`, kill proc) | Lets a least-privilege admin account operate via stored procedures. |
| 12 | Private reachability | ARCHITECTURE "MySQL: private" | ClusterIP only (in-cluster) or managed with trusted sources = the cluster. |

## 2. Mapping to PostgreSQL, with difficulty

| # | PostgreSQL mechanism | Verdict |
|---|---|---|
| 1 Per-app isolation | `CREATE ROLE app_N LOGIN PASSWORD ... CONNECTION LIMIT n`; `CREATE DATABASE app_N OWNER app_N`; then `REVOKE ALL ON DATABASE app_N FROM PUBLIC` (PUBLIC has CONNECT and TEMP by default, which is the single biggest gotcha: without this every role can connect to every database); grant CONNECT only to `app_N`. Cross-database queries do not exist in Postgres, so databases are isolated from each other by construction. Schema: PG 15+ removed CREATE on `public` from PUBLIC; on PG 14 and older we must `REVOKE CREATE ON SCHEMA public FROM PUBLIC` ourselves, and because `app_N` owns the DB it owns its objects. Also set per-role defaults `ALTER ROLE ... SET search_path`. Known leak: any role can read the catalog (`pg_database`, `pg_roles`) and so sees other apps' names (`app_<id>`); low severity, no data. | **Easy**, with a checklist of default-privilege gotchas. Identifier validation: use `psycopg.sql.Identifier`, which does bind identifiers, so it is safer than MySQL's regex-and-f-string. |
| 2 Provider abstraction | New `PostgresAdmin` implementing the same operations. The ABC is currently named `MySQLAdmin` with MySQL-flavored errors; rename or generalize to `DbAdmin` (mechanical). | **Easy** |
| 3/4 Placement, bootstrap | Add `DbServer.engine` (`mysql`\|`postgres`, default `mysql`); `choose_server(session, engine)` filters by it; add `BOOTSTRAP_PG_*`. | **Easy** |
| 5 TLS | DO managed Postgres enforces TLS. Clients use `sslmode=require` (encrypted, unauthenticated) or `verify-ca`/`verify-full` with `sslrootcert=<file>`. libpq wants a **file path** for the CA, so apps that want verification must write `${{ db.ca_cert }}` to disk, or the app image's runtime must accept inline PEM (many Node/Python drivers do). Same shape as the Grafana "skip-verify vs CA bundle" tradeoff already documented. In-cluster: self-signed or cert-manager cert, same refs. | **Easy** (docs-heavy). Add `db.sslmode` (`require`/`disable`) to env refs. |
| 6 Egress netpol | Same policy with port 5432 (in-cluster) or the DO-assigned port (DO uses non-standard ports such as 25060, as it does for MySQL; use `DbServer.port`). Rename `allow-egress-mysql` to `allow-egress-db`; peer selector `app=postgres` for in-cluster. An app has exactly one DB, so each tenant namespace still gets exactly one DB egress rule. | **Easy** |
| 7 Connection cap | `ALTER ROLE app_N CONNECTION LIMIT n`. Idempotent re-apply is trivial. Caveat: it counts only logins, and superusers are exempt (not our tenants). Capacity caveat: a 1 GB managed instance has a small global `max_connections` (roughly 20-25 usable; confirm in the DO console), so 10 per role oversubscribes quickly. Mitigation: lower the Postgres default (for example 5) and/or use DO's built-in connection pool (PgBouncer, transaction mode) per app; pooled mode breaks session features (prepared statements, `SET`, advisory locks) so keep direct as the default. | **Easy to enforce, hard to size.** |
| 8 Size quota | See the dedicated section below. | **HARD** |
| 9 Credential rotation | `ALTER ROLE app_N PASSWORD ...` (use SCRAM; PG 14+ default). | **Easy** |
| 10 PK constraint | Postgres has no equivalent server-level setting. Wiki.js-class apps just work. | **Upside, free.** |
| 11 Restricted admin | DO's `doadmin` is not a true superuser (it has CREATEROLE/CREATEDB). In PG 16+, a CREATEROLE role gets ADMIN on roles it creates but not automatic INHERIT/SET, and `ALTER DATABASE ... OWNER TO app_N` requires membership/SET on the target role. Needs a spike on the real DO service: confirm `doadmin` can create role, create database owned by it, terminate its backends (`pg_terminate_backend` requires membership in the target role or `pg_signal_backend`), and `ALTER ROLE ... SET`. | **Medium, needs a live spike** |
| 12 Private reachability | Identical: DO trusted sources / ClusterIP. | **Easy** |

Additional Postgres-only hardening available (gains over MySQL): per-role `ALTER ROLE ... SET statement_timeout`,
`idle_in_transaction_session_timeout`, and `temp_file_limit` (superuser-context settings that a tenant cannot override
once set by an admin, if DO lets `doadmin` set them; verify). `temp_file_limit` closes the "fill the disk with temp
spill" hole that a data-size cap does not see. Extensions: only trusted/allow-listed ones; DO restricts the rest.

## 3. The hard part: size quota (50 MB per database)

**What MySQL does and why it is strong.** The app user holds no GRANT OPTION and does not own the privilege system, so a
REVOKE of INSERT/UPDATE/CREATE/ALTER cannot be undone by the tenant; killing sessions makes it take effect immediately.

**Why Postgres is harder.**
1. No native per-database or per-role disk quota (no equivalent of a quota in the engine). Disk quota needs filesystem
   or tablespace tricks (a tablespace per app on its own volume), far too heavy for a 50 MB cap.
2. Measuring is easy: `SELECT pg_database_size('app_N')` (includes indexes and TOAST; excludes WAL and temp files). It is
   an accurate, cheap, and current number (no stats-expiry gotcha like MySQL 8). Fine to poll every 60 s.
3. **Enforcing is the problem: the tenant role owns its objects.** In Postgres the owner of a table can `GRANT` and
   `REVOKE` on it, so a `REVOKE INSERT ... FROM app_N` on tables the role owns is undone by the owner (and revoking from
   an owner is mostly ignored: owners keep implicit rights to GRANT themselves back). If instead a separate platform-owned
   role owns the objects and `app_N` only gets DML grants, then the app cannot run DDL migrations (Ghost, Grafana and
   most apps migrate on boot), and making `app_N` a member of the owner role hands the ownership back to it.

**Options considered.**

| Option | Strength | Cost / problems |
|---|---|---|
| A. `ALTER DATABASE app_N SET default_transaction_read_only = on` (+ same on the role) and terminate backends | Stops every well-behaved app immediately (new sessions start read-only; terminated sessions reconnect read-only). Reversible with a single `RESET`. Allows reads. Per-session override: **a tenant can run `SET default_transaction_read_only = off` or `BEGIN READ WRITE`** (it is a user-settable GUC), so it is a speed bump for a hostile tenant, not a hard boundary. Cleanup works (the user can switch to read-write to DELETE), which is arguably a feature. | Weak against intentional abuse. |
| B. REVOKE on tables / schema CREATE | Strong only if the tenant is not the owner. | Conflicts with migrations and ownership (see above). Not workable as the primary mechanism. |
| C. Hard lockout: `REVOKE CONNECT ON DATABASE app_N FROM app_N` (or `ALTER ROLE ... NOLOGIN`) + `pg_terminate_backend` | Strong and tenant-proof (the tenant cannot grant itself CONNECT: the DB owner can! see below). | App fully down, including reads, and the tenant cannot delete data to recover without an admin step. If the app role owns the DB it could also re-grant itself CONNECT, so ownership of the **database** should stay with the admin role (the app role owns the schema and objects inside, not the database), which is workable: `CREATE DATABASE app_N OWNER admin`, then `GRANT CONNECT, TEMP`, `CREATE ON SCHEMA public` to `app_N`, and `ALTER SCHEMA public OWNER TO app_N`. |
| D. Per-app tablespace on a quota'd volume or XFS project quotas | Truly hard cap, in-cluster only. | One PVC/mount per app; massive complexity; not on managed. Rejected. |
| E. Trigger/event-trigger enforcement | Cannot see total size cheaply; adds per-write overhead; tenant can drop its own triggers. Rejected. |

**Recommendation: a two-stage hybrid (A then C), with the DB owned by the admin role.**
- Stage 1 (>= 100% of 50 MB): option A (database- and role-level `default_transaction_read_only = on`), then
  `pg_terminate_backend` for the role's sessions so pooled connections reconnect read-only; optional pod bounce, same as
  MySQL today. Cooperative apps (Ghost-class, all current recipes) stop growing; users can still read and (by switching
  the session to read-write) delete to get back under. State is recorded in `AppDatabase.over_quota` exactly as now.
- Stage 2 (>= a hard ceiling such as 150% of the cap, to catch a tenant that overrides stage 1): option C, revoke CONNECT
  and terminate sessions, mark the app's DB `locked`, surface the reason in the UI, and require an admin or an explicit
  "unlock for cleanup" action. Because the poll is every 60 s, the worst case overshoot is bounded by what a tenant can
  write in about a minute, which is the same exposure the MySQL path has today (it already showed 300 MB under a 50 MB cap
  before the kill step existed).
- Honest summary: against a **cooperative or merely careless** tenant this matches MySQL's behavior; against a
  **hostile** one it is weaker at stage 1 and falls back to a blunt lockout at stage 2, where MySQL stays graceful (reads
  and deletes still work). It also cannot be perfect on managed until the spike confirms `doadmin` can terminate backends
  and alter database/role settings. Because the platform is admin-approval-gated for signups, this residual risk is
  acceptable for a pilot but should be called out in docs.
- Also note bloat: `DELETE` does not shrink `pg_database_size` until VACUUM FULL / `DROP`/`TRUNCATE`; users who delete
  rows to get under may stay "over" until autovacuum plus table rewrite. MySQL/InnoDB has a similar (smaller) effect.
  Document the guidance: `TRUNCATE` or `VACUUM FULL`.

## 4. What extending the platform would take (outline, no code)

1. **Provider.** `app/providers/postgres.py::PostgresAdmin` implementing the generalized `DbAdmin` ABC (rename
   `MySQLAdmin`; keep an alias during transition). Driver: `psycopg` (3) with `psycopg.sql.Identifier`. Fake in
   `fake.py` gets an engine-parameterized variant so tests stay in-memory. Per-op: `create_db_user` (role, database,
   REVOKE FROM PUBLIC, limits, timeouts), `drop` (terminate sessions, `DROP DATABASE ... WITH (FORCE)`, `DROP ROLE`),
   `db_size_bytes` (`pg_database_size`), `set_read_only` (stages A and C above), `set_password`, `server_health`
   (`pg_stat_database`, connection count vs `max_connections`).
2. **Model.** `DbServer.engine` and `AppDatabase.engine` (denormalized; immutable once created), Alembic migration with
   `engine` default `mysql`. `choose_server` takes an engine. A registry that returns the right admin class per server.
3. **Config.** `BOOTSTRAP_PG_*`, `PG_EGRESS_MODE`/`_CIDR`, `PG_MAX_USER_CONNECTIONS` (separate, lower default),
   `PG_ADMIN_...`. The quota constants stay engine-agnostic.
4. **Env refs.** `app/env_refs.py`: add `db.engine`; make `db.url` engine-correct (`postgres://user:pass@host:port/db`
   with `?sslmode=require` when TLS is on); add `db.sslmode`; keep `db.ca_cert`/`db.tls`. Note that many Postgres apps
   expect `DATABASE_URL`, with some wanting the `postgresql://` scheme; offer `${{ db.url }}` as `postgresql://` and
   document alternatives. A single-ref rule for `db.url` already exists (Grafana recipe).
5. **Egress.** `network_policies(...)`: generalize `allow-egress-mysql` to `allow-egress-db` with an engine-specific
   in-cluster peer selector and port; reconciler passes the app's server engine/host/port.
6. **Quota.** `app/quota.py::_poll_db` calls the generalized `DbAdmin`; add the `locked` stage (new state on
   `AppDatabase`, or a second flag) and UI surfacing on the Health and app pages.
7. **Selecting an engine.** Spec/API: `db: true` keeps meaning MySQL (back-compat); new `db: {engine: postgres}` in the
   `picopaas/v1` spec and `engine` on `POST /apps`; UI radio on the app form; engine is immutable after creation (changing
   means create a new DB and migrate data). Limit of one DB per app stays (so one engine per app).
8. **Platform-side.** Health page `PLATFORM_NAMESPACES` and server cards need engine-aware rows; `app/db_host_metrics.py`
   targets DO's MySQL metrics endpoint, so a Postgres equivalent is needed. Backups: managed has daily PITR; in-cluster
   needs a `pg_dump` CronJob to Spaces (mirror `mysql-backup`).
9. **Docs/tests.** USER_GUIDE section 6 gets a Postgres sibling; RECIPES gain Postgres examples (Listmonk, Miniflux, n8n);
   isolation tests (cross-DB CONNECT denied, PUBLIC revoked, over-quota stages, role limit) against a real Postgres in CI
   (service container), mirroring how MySQL is tested.
10. **Spike first (needs a real server, so a paid or in-cluster instance).** Verify the `doadmin` capabilities listed in
    row 11, the `temp_file_limit`/`statement_timeout` per-role settings, the `max_connections` on the 1 GB plan, and the
    stage 1 / stage 2 behavior against a hostile test tenant.

## 4b. Managed vs in-cluster, and cost

| | DO Managed PostgreSQL | In-cluster Postgres (StatefulSet in `picopaas-data`) |
|---|---|---|
| Static cost | about $15/mo (`db-s-1vcpu-1gb` single node; confirm current price). New paid resource, needs approval. | about $1-2/mo PVC; consumes node RAM/CPU (256-512 MB, 100-250m), possibly pulling the second node (+~$28) up more often. |
| Reliability | Managed patching, daily backups with PITR, failover available at a higher tier. | We own patching, backups (CronJob to Spaces), restore drills, upgrades. PVC is single-AZ; node loss = outage. |
| Isolation work | `doadmin` is not a superuser: some operations need the spike (row 11). TLS and trusted sources provided. | Full superuser: easiest to implement strong quota (could even use per-app tablespace experiments) and `temp_file_limit` etc. Same pattern as the existing in-cluster MySQL variant. |
| Connections | Small global limit on 1 GB; DO PgBouncer pools are included. | We set `max_connections` (default 100) subject to RAM. |
| Fits the budget | Pushes about $60 to about $75 (about $103 with the second node). | Fits the existing cap. |

**Recommendation.** Approve the **in-cluster pilot first** (almost free, full superuser makes the isolation and quota
spike straightforward, reuses the existing in-cluster egress path), build the provider against it, and prove Stage 1 and
Stage 2 quota behavior. If real users adopt it, request the managed instance (a follow-up budget request) for backups and
operations, since an in-cluster database on a single node is a poor home for anyone's real data. The provider is
identical in both; only `DbServer` rows, egress mode, and a managed-only permission spike differ.

## 5. Should we ditch MySQL for Postgres entirely? (options, not a decision)

**What Postgres unlocks.** Postgres-only or Postgres-first apps we cannot run today: n8n (SQLite or Postgres in prod),
Listmonk and Miniflux (Postgres only), Outline (Postgres, but also needs Redis and S3-style storage, so Postgres alone does
not unblock it), Wiki.js (the primary-key clash disappears; it supports Postgres), plus Gitea/Forgejo, Vaultwarden,
Metabase, Directus, Strapi, Umami, Nextcloud and others that support Postgres as well as MySQL. Grafana and Shlink also
support Postgres.

**Cost and risk of leaving MySQL.**
- **Ghost cannot move.** Ghost supports MySQL 8 (and SQLite in dev), not Postgres. Our own devlog (blog.picopaas.xyz,
  app 98) runs on it, and Ghost is a flagship example. A Postgres-only platform would drop Ghost support.
- **Existing apps need data migration.** Ghost/Grafana/Shlink data would need export/import with dialect differences
  (types, case sensitivity, auto-increment vs sequences). Grafana and Shlink are the easier ones (both support Postgres
  natively; switch the connection and migrate or re-seed). No automated path in the platform; per-app manual work, user
  downtime, and rollback risk.
- **The platform's own database.** The control plane itself uses MySQL in prod (SQLAlchemy/Alembic, with MySQL-specific
  bits such as `GET_LOCK('picopaas-reconciler')` in the scheduler and MySQL-flavored migrations). Moving it means porting
  to `pg_advisory_lock`, re-validating every migration, and a one-time metadata cutover.
- **Quota semantics regress** for hostile tenants (section 3), so a Postgres-only platform bets everything on the weaker
  enforcement.
- **Test and CI churn:** the e2e quota suite and `db_host_metrics` are MySQL-specific.

**Options.**
1. **Add Postgres alongside MySQL (recommended).** Additive, reversible, lets the platform keep Ghost and unlock the
   Postgres-only apps. Cost: two providers, two quota paths and two TLS/egress variants to maintain and test; each is
   small because they sit behind one interface and one engine column.
2. **Postgres-only (long-term cleaner).** One engine, one quota path, one docs set, one CI. Large one-time lift
   (control-plane port, three app migrations, losing Ghost), and the quota regression. Only sensible once Postgres has
   proven itself in production and Ghost is no longer a required workload.
3. **MySQL-only (status quo).** Zero effort; stays blocked on every Postgres-only app.

**Recommendation.** Do option 1 now (pilot in-cluster), keep MySQL, and revisit ditching MySQL only after about a quarter
of Postgres in production, when we know the real quota-enforcement behavior and how many users picked each engine. This is
Chas's call; nothing above is a decision.

## 6. Open questions for Chas (not yet copied into docs/QUESTIONS.md; out of scope for this chunk)
- Approve the in-cluster pilot (about $0-2/mo)? Approve managed (about $15/mo, exceeds the approximate $60 cap) later?
- Is the weaker-than-MySQL stage 1 quota behavior acceptable given approval-gated signups?
- Keep Ghost as a first-class workload (which blocks Postgres-only)?
