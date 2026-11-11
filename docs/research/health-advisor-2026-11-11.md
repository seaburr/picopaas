# Rule-based health advisor: research and design (2026-11-11)

Status: research only. No code, cluster or config was changed. Role: sre.

Question from Chas: something that watches app health and resource use and suggests changes, with **no LLM**
(background model calls cost money). Rules, statistics and heuristics only.

Short answer: yes, and most of it is nearly free. PicoPaaS already collects the inputs for about a dozen useful rules.
Phase 1 needs no new data source, no new table and no new cluster calls. Right-sizing in the VPA/KRR sense needs
longer history than we keep (3 hours today), which one small rollup table fixes. True CPU throttling and true
"idle" need one new data source each and are the only parts that touch the cluster.

---

## 1. What PicoPaaS collects today

All verified in the code on 2026-11-11. Caps quoted from `app/config.py`.

| Signal | Where | Cadence | Retention | Notes |
|---|---|---|---|---|
| Pod CPU (m) and memory (MiB) | `health.sample_app_metrics` → `app_metric_samples` | 60 s (`METRICS_SAMPLE_SECONDS`) | newest 180 points per app = **3 hours** (`METRICS_RETENTION_POINTS`) | From the cached cluster snapshot (metrics.k8s.io via metrics-server, refreshed every 15 s). Row holds the sum over pods plus `per_pod` JSON. No extra metrics-server call. Suspended apps are skipped. |
| Pod phase, ready, restart count | `KubeProvider.cluster_snapshot` | 15 s cache | none (live only) | `ready` and `restarts` per pod are in the snapshot but not persisted. |
| Restart events | `reconciler.record_app_events` → `app_events` | every reconcile (15 s) | newest **20 per app** | Kinds: `oom`, `crashed` (with exit code), `disk` (scratch-disk eviction), `evicted`, plus `backup_failed` and auto-rollback. Deduped per occurrence. Owner can dismiss. |
| App status and reason | `apps.status`, `status_reason`, `status_detail`, `error_count`, `last_attempt_at` | reconcile | current value only | Reasons include `image_pull`, `crash_loop`, `oom`, `not_ready`, `quota`, `timeout`. |
| DB size | `quota.poll_quotas` → `app_databases.size_bytes`, `over_quota` | 60 s | current value only | Quota 50 MB default (`DB_QUOTA_BYTES`), admin override per app. |
| Bucket size | same → `app_storage.size_bytes`, `over_quota` | 300 s | current value only | Quota 100 MB default; owner can lower, admin can raise. |
| Volume used bytes | `volume_stat.tick` → `apps.volume_used_bytes` | at most every 900 s per volume (a stat Job) | current value only | Volume 1 GiB, admin grow-only to 10 GiB. |
| Image | `apps.image`, `image_digest`, `rejected_digest`, `image_update_error`; `app_image_history` (5 per app) | image watch every 10 min | 5 digests | Platform pins the digest and auto-rolls back a digest that fails to start. |
| Health check config | `apps.health_check_enabled`, `health_check_path`, `health_check_timeout_seconds` | n/a | n/a | Disabled = TCP readiness probe only, no liveness probe. |
| Backups | `app_backup_configs.last_status`, `last_success_at`, per-kind fields | per run | current value only | |
| Resources | `resources.effective(app)` | n/a | n/a | Defaults: CPU limit 200 m (request 64 m), memory limit 384 MiB (request 128 MiB), scratch 250 MiB. Owner may lower within floors (CPU 50 m, memory 64 MiB; 128 MiB with a volume). Admin override up to the node caps (1500 m, 2560 MiB). |
| Platform capacity | `quota.capacity_summary`, `admin.py` (`WARN_PCT = 80`) | on request | none | DB server slots and disk already warn at 80%. |

Not collected today:

- **CPU throttling.** metrics-server reports usage only. The ground truth (`nr_throttled`, `throttled_usec` in the
  container cgroup `cpu.stat`) is not read by anything.
- **Requests, status codes, latency per app.** No scrape of Traefik. So no real "idle" and no error rate.
- **History beyond 3 hours** for CPU/memory, and **any** history for DB, bucket and volume size.
- **DB connections actually used** per app.

Two facts shape the design:

1. **The scheduler places pods by request (64 m / 128 MiB), not by limit.** An owner lowering a memory limit from
   384 to 200 MiB frees nothing on the node unless it goes below 128. There is also no per-app bill. So "you could
   lower this" is weak advice for an owner here. It is useful to the admin as an overcommit picture.
2. **Limits apply per pod, but `cpu_m`/`mem_mi` are sums over pods.** During a rolling update two pods exist and the
   sum doubles. Every rule below must use the per-pod maximum from `per_pod`, never the sum.

---

## 2. What others do (sources at the end)

### Right-sizing engines

| Tool | Method | Numbers | Needs |
|---|---|---|---|
| **VPA recommender** | Exponentially decaying histograms of usage. Target, lower and upper bound are percentiles plus a safety margin. Memory uses the peak per aggregation interval. An OOM bumps the memory sample. | Target p90, lower p50, upper p95 (CPU and memory). Margin 15%. Half-life 24 h. Memory peak per 24 h window, 8 windows. History 8 days. OOM bump ×1.2 or +100 MiB, whichever is larger. Floors 25 m and 250 MB per pod. Runs every 1 min. [V1] | metrics.k8s.io; optional Prometheus for history |
| **Goldilocks** | Does no maths itself. Creates VPA objects and shows the result as two options: "Guaranteed" (request = limit = target) and "Burstable" (request = lower bound, limit = upper bound). Says it needs 8 days for full accuracy and that the upper bound is wildly high early on. [G1] | as VPA | VPA + metrics-server |
| **KRR (Robusta)** | Plain statistics over Prometheus history, no in-cluster agent. | CPU request = p95, **no CPU limit**. Memory = max over the window + 15%. History 7 to 14 days. [K1] | Prometheus, cAdvisor, kube-state-metrics |
| **Kubecost** | Recommendation = observed usage ÷ target utilisation. Algorithm "Max" by default, or a percentile. Profiles (dev, production, HA) differ only in target utilisation. Has a one-click "Resize Requests Now" with a confirm step. Floors 10 m / 20 Mi. Example: max 1.2 CPU at target 0.8 → 1.5 CPU. [C1] | Prometheus |
| **OpenCost** | No recommendations. Defines workload cost as `max(request, usage)` and idle as allocated minus used. [O1] | Prometheus |

Take-aways: nobody uses anything smarter than a percentile or a maximum plus a margin of 15 to 25%. All of them
want **7 to 14 days** of history. Memory is sized on the **peak**, CPU on a **high percentile**. An OOM is treated as
direct evidence and bypasses the statistics. Kubecost is the only one with an apply button, and it confirms first.

### How small PaaS products surface this

| Product | What the user gets | Thresholds |
|---|---|---|
| **Render** | Email/Slack on failed deploy, service unhealthy, image pull failure, and disk over 80%. A service that keeps failing to start for 24 h is suspended. No sizing advice. [R1] | Disk 80% |
| **Railway** | Metrics tab (30 days). User-defined monitors on CPU, RAM, disk, egress (Pro plan). Webhooks for crashes and deploys. Right-sizing is a written guide, done by hand: memory limit 1.5 to 2× observed peak; watch for memory slope that only resets on deploy (leak); CPU high while network is quiet (busy loop). [RW1][RW2] | Manual |
| **Fly.io** | Metrics only: `fly_instance_exit_oom`, `fly_instance_cpu_throttle`, CPU balance. "Fly.io doesn't include built-in alerting on metrics". No suggestions. [F1] | None |
| **Northflank** | Fixed-threshold alerts: CPU or memory at 90% (spike), 90% for 5 min (sustained), container crash (bad exit code, OOM, continual restarts), eviction for scratch disk, volume at 75% and 90%. One alert per resource per time window. [N1] | 90% / 5 min; 75% and 90% |
| **Coolify** | Notifies on unexpected container stop (rechecks after 2 min, at most one per 15 min per resource), restart limit reached, server disk over a set threshold (repeats every 24 h), backup failure (first failure, then at most daily, one "works again"). [CO1] | Debounce rules |

Take-aways: the field does **alerts with fixed thresholds**, not advice. None of them tells the user what to change.
A card that says "raise memory from 256 to 320 MB" with the evidence next to it would be ahead of all five, and it
needs no more data than their alerts do. Their debounce rules (one per window, recheck before alerting, "works
again" once) are worth copying.

### Heuristics that are enough

- **Percentile and peak over a window.** The whole of VPA/KRR/Kubecost in practice.
- **Sustained threshold**: value ≥ X% of limit for N consecutive samples (Northflank: 90%, 5 min).
- **Restart rate.** Kubernetes backs off 10 s, 20 s, 40 s … capped at 300 s and resets after 10 min of clean running
  [KB1]. So 3 restarts inside 10 min is already a loop; 3 spread over a day is flapping.
- **EWMA baseline**: `EWMA_t = λ·Y_t + (1−λ)·EWMA_(t−1)`, λ usually 0.2 to 0.3, flag outside ±3·s·√(λ/(2−λ))
  [E1]. Useful for "CPU is far above this app's own normal". Needs hourly history.
- **Linear trend**: least-squares slope over hourly or daily points for "memory climbs since the last restart" (leak)
  and "DB full in N days" (forecast). Require a minimum fit (R² ≥ 0.8) and a minimum span to stay quiet.
- **Change correlation**: a restart burst that starts within 30 min of a new image digest points at the deploy.
  We have both timestamps.

---

## 3. Proposed advisor

### Shape

- One module, `app/advisor.py`. A rule is a pure function `(AppFacts) -> Finding | None`. `AppFacts` is a plain
  struct filled from rows we already have. No I/O in rules, so every rule is a table-driven unit test.
- `Finding` = `rule` (stable id), `severity` (`critical` | `warning` | `info`), `title`, `evidence` (numbers and
  window), `action` (optional: `kind`, `field`, `from`, `to`, `restarts_app`, `who` = owner | admin).
- Evaluation is deterministic and cheap: comparisons, a sort for percentiles, one least-squares fit. No model, no
  network.
- The advisor **never changes anything by itself**. It only proposes.

### Rules

Severity: C = critical, W = warning, I = info. "Have it" = data exists today.

**Phase 1 rules (existing data only)**

| Id | Condition | Sev | Suggestion | Data | Have it |
|---|---|---|---|---|---|
| `mem.oom` | ≥ 1 `oom` event in 24 h (W); ≥ 3 in 24 h (C) | W/C | Raise memory to `min(ceiling, round_up_32(limit × 1.25))`. At the ceiling: "at the platform cap (384 MB); reduce usage or ask an admin". Admin view offers the override up to the node cap. | `app_events`, `resources.effective/ceilings` | yes |
| `mem.near_limit` | per-pod memory ≥ 90% of limit for ≥ 5 consecutive samples (5 min) in the last 3 h, and no `mem.oom` open | W | Same raise as above. "Likely to be OOM-killed." | `app_metric_samples.per_pod` | yes |
| `cpu.saturated` | per-pod CPU ≥ 90% of limit for ≥ 5 consecutive samples, or in ≥ 10% of samples in the last 3 h | W | Raise CPU to the ceiling if the owner lowered it; otherwise admin override. Text says "pinned at its CPU limit, so it is being throttled" (a proxy, see 3.3). | samples | yes (proxy) |
| `restart.loop` | `status_reason = crash_loop`, or ≥ 3 `crashed` events within 10 min | C | "Check the logs." If a new digest went live ≤ 30 min before the first crash: "Started after image update `abc123` → `def456`. Roll back." (existing `POST /apps/{id}/rollback`) | `app_events`, `app_image_history` | yes |
| `restart.flapping` | ≥ 3 `crashed` events in 24 h, not a loop | W | "Exited N times in 24 h with code X. Check the logs." | `app_events` | yes |
| `disk.scratch` | ≥ 1 `disk` event in 7 d | W | Raise scratch disk (owner, if below ceiling) or "write to the bucket or a volume; scratch files are lost on restart". | `app_events` | yes |
| `db.near_quota` | size ≥ 80% of quota (W), ≥ 95% (C); `over_quota` (C) | W/C | "Delete data or ask an admin to raise the quota." Admin view offers the override. | `app_databases` | yes |
| `bucket.near_quota` | same thresholds | W/C | Owner raise if below ceiling, else admin. | `app_storage` | yes |
| `volume.near_full` | `volume_used_bytes` ≥ 80% (W), ≥ 90% (C) of size | W/C | Admin: grow volume (grow-only, costs money per GiB: see cost note). | `apps.volume_*` | yes |
| `addon.unused` | add-on attached ≥ 7 d and DB size = 0 / bucket size = 0 / volume used < 1 MB | I | "Database attached 12 d ago, still empty. Detach it if unused." Frees a DB-server slot or a volume (`VOLUMES_MAX_TOTAL = 10`). | sizes + created timestamps | yes (size only; see 3.3) |
| `image.mutable_tag` | tag is `latest` or absent, and the ref is not a digest | I | "Tag `latest` moves. Every push redeploys within 10 min. Use a version tag to control when updates ship." | `image_check.image_tag` | yes |
| `image.rejected` | `rejected_digest` set or `image_update_error` set | W | "Newest image failed to start; running the previous one." | `apps` | yes |
| `health.none` | `health_check_enabled = false` and app running ≥ 24 h | I | "Only a TCP check on port N. A hung process is not restarted. Enable an HTTP health check." Link to Settings. | `apps` | yes |
| `health.failing` | `status_reason` in (`not_ready`, timeout reasons), or a pod `ready = false` while `phase = Running` | C | "Health check on `/healthz` is not returning 2xx/3xx." | `apps`, snapshot | yes |
| `backup.failing` | `last_status = failed`, or `last_success_at` older than 2× the interval | W | "Last good backup 3 d ago: <last_error>." | `app_backup_configs` | yes |
| `app.stuck_error` | `status = error` for ≥ 7 d | I (admin only) | "In error for 9 d (`image_pull`). Suspend or delete." | `apps` | yes |

**Phase 2 rules (need the hourly rollup, section 3.2)**

| Id | Condition | Sev | Suggestion |
|---|---|---|---|
| `mem.headroom_low` | 7-day per-pod peak ≥ 85% of limit, no OOM yet | W | Raise to `round_up_32(peak × 1.25)`, capped at the ceiling. This is the "before it OOMs" version of `mem.oom`. |
| `mem.oversized` | ≥ 5 days of data, no `oom` in 14 d, and `round_up_32(max(peak_7d × 1.3, floor))` ≤ 60% of limit | I | Owner: optional lower limit ("contains a leak earlier; does not free node capacity"). Admin: counted in the overcommit view. Margin 1.3 sits between KRR's 1.15 and Railway's 1.5 to 2. |
| `mem.leak` | hourly max since the last restart rises with a positive least-squares slope, R² ≥ 0.8 over ≥ 12 h, and reaches the limit within 48 h at that slope | W | "Memory grows about 6 MB/h since the last restart; reaches 384 MB in about 19 h." |
| `quota.forecast` | least-squares slope of daily DB / bucket / volume size over ≥ 5 d reaches the quota within 14 d | W | "Database grows about 3 MB/day; full in about 6 d." |
| `cpu.anomaly` | hourly CPU p95 above EWMA (λ = 0.25) + 3σ for 3 consecutive hours | I | "CPU is 4× this app's 7-day normal." No action button. |
| `restart.regression` | restarts in the last 24 h ≥ 3 and ≥ 3× the prior 7-day daily mean | W | Text only, with the image-change correlation when it applies. |
| `node.overcommit` (admin) | sum of per-app 7-day memory peaks ≥ 80% of node allocatable, or sum of p95 > sum of requests × 1.5 | W | "Apps use more than they request; a busy hour can evict pods. Raise the default request or add a node (needs a request in `docs/requests/`)." |

**Phase 3 rules (need a new data source, section 3.3)**

| Id | Condition | Needs |
|---|---|---|
| `cpu.throttled` | throttled periods ÷ total periods ≥ 25% over 15 min | kubelet cAdvisor counters |
| `app.idle` | 0 requests for ≥ 14 d (I), ≥ 30 d (admin: suggest suspend) | Traefik request counters |
| `http.errors` | 5xx share ≥ 5% over 15 min with ≥ 20 requests; or EWMA of 5xx share above baseline + 3σ | Traefik request counters |
| `db.never_connected` | no connection by the app's DB user since attach | MySQL `performance_schema.accounts` / PostgreSQL `pg_stat_database` |

### Guard rails (apply to every rule)

- Skip suspended apps, apps younger than 1 h, and the first 10 min after a pod start (start-up spikes).
- One open finding per resource. "Raise" beats "lower". Never suggest lowering within 24 h of a restart or within
  14 d of an OOM.
- Round suggestions to steps of 32 MiB and 50 m, clamp to `resources.floors` and `resources.ceilings`. The advisor
  must import caps from `app/config.py` and never restate them.
- Debounce like Coolify and Northflank: a finding opens when its condition holds, closes after it has been false for
  two evaluations, and a dismissed finding returns only on a new occurrence (same semantics as today's event
  dismiss).
- State what is measured. `cpu.saturated` says "pinned at its limit", not "throttled N%", until phase 3.

### 3.1 Where it runs and what it costs

| Phase | Extra cluster calls | Extra DB load | Extra pod CPU/memory | $/month |
|---|---|---|---|---|
| 1 | **none** (reads rows and the cached snapshot) | read ≤ 180 sample rows + ≤ 20 events per app per evaluation | microseconds per rule; negligible | 0 |
| 2 | none | one hourly aggregation; 336 rows per app (14 d × 24). 50 apps ≈ 17 k rows, roughly 1 to 2 MB with the index | negligible | 0 |
| 3 throttling | one kubelet `/metrics/cadvisor` read per node every 5 min. The body is large (order of MB of text per node); parsing costs tens of ms of CPU per scrape in the platform pod (limit 250 m / 384 MiB) | 2 integers per app per hour in the rollup | small but not zero; measure before enabling | 0 |
| 3 traffic | one Traefik `/metrics` read per Traefik pod every 5 min | 3 integers per app per hour | small | 0 |

No Prometheus, no VPA, no new pods at any phase. All jobs are leader-only like `metrics_sample_job`.

### 3.2 Storage

Options:

| Option | What | Verdict |
|---|---|---|
| None | Evaluate on request from current rows | Enough for phase 1. No dismiss, no "since", marker costs a query per app. |
| Keep more raw samples | Raise `METRICS_RETENTION_POINTS` to 14 d | No. 20 160 rows per app with a JSON column; 50 apps ≈ 1 M rows, and the per-app prune runs every minute. |
| **Hourly rollup** | `app_metric_hourly(app_id, hour, n, cpu_p50_m, cpu_p95_m, cpu_max_m, mem_p95_mi, mem_max_mi, restarts, db_bytes, bucket_bytes, volume_bytes)`, per-pod maxima, 14 d kept | **Recommended for phase 2.** Built hourly from the raw samples (3 h raw retention covers a missed run). Also gives size history for forecasts and a 14-day chart for free. |
| Findings table | `app_advice(app_id, rule, severity, since, last_seen, detail_json, dismissed_at, fingerprint)` | Phase 2. Gives "since", dismiss, hysteresis, and a single indexed query for the apps-table marker and the admin list. |

Recommendation: phase 1 with **no new table** (compute on request, cache per app for 60 s). Phase 2 adds both tables
in one migration.

### 3.3 Data gaps and how to close them

- **CPU throttling.** Phase 1 uses "usage ≥ 90% of limit" as a proxy. It is sound in one direction: an app averaging
  90% of its quota over the metrics window is certainly being throttled. It misses bursty or multi-threaded apps that
  throttle hard at a low average. Ground truth is `container_cpu_cfs_throttled_periods_total` and
  `container_cpu_cfs_periods_total` from the kubelet. Access path matters: going through the API server
  (`/api/v1/nodes/<n>/proxy/metrics/cadvisor`) needs `get` on `nodes/proxy`, and Kubernetes warns that this "is not a
  read-only permission, and authorizes executing commands in any container running on the node" [KA1]. Do not grant
  that to the platform. The safe path is the narrower `nodes/metrics` subresource with a direct call to the kubelet
  on port 10250 (what metrics-server does). Whether pods can reach node port 10250 on DOKS is **not verified**. This
  is an RBAC change: needs Chas's go-ahead and a security review.
- **Requests, errors, idle.** Traefik exports per-service request counters by status code. `deploy/traefik/values.yaml`
  does not mention metrics; the chart may already expose them on its internal metrics port by default. **Not
  verified.** If it does, a scrape every 5 min is a NetworkPolicy and code change only. If not, it is a Traefik values
  change (rolls Traefik: needs go-ahead).
- **DB actually used.** Size = 0 is a weak signal (an app can connect and create nothing; MySQL size counts tables
  only). Connection counts per DB user exist in both engines' statistics views; whether the managed MySQL exposes
  them to our admin user is **not verified**.
- **Long history.** Closed by the rollup.

### 3.4 Presentation

Follows the house rules: technician-first wording, text only, no icons, same pattern everywhere.

**App health page (`/apps/:id`)**: an "Advice" card above the charts, shown only when there are findings. One row per
finding: severity word, title, evidence, action. Example rows:

```
Critical  Out of memory            OOM-killed 3 times in 24 h (last 14:02 UTC). Limit 256 MB,   [Raise memory to 320 MB]
                                   peak 251 MB.
Warning   CPU pinned at limit      At or above 90% of 200 m in 31 of 180 samples (last 3 h).    Ask an admin: at the platform cap
Warning   Database near quota      41.3 of 50 MB (83%).                                         [Open database usage]
Info      Mutable image tag        ghcr.io/x/y:latest. Every push redeploys within 10 min.      [Edit image]
Info      No HTTP health check     TCP check on port 8080 only. A hung process is not restarted. [Open settings]
```

Each row has "Dismiss" (phase 2) and a rule id in the row's title attribute for support. The existing restart
alert banner stays; when `mem.oom` or `restart.loop` is open, the banner links to the card instead of repeating it.

**Apps table**: in the Health column, a plain text count after the status, for `warning` and `critical` only, for
example `running · 2 advice`. `info` findings never mark the table (otherwise every `latest` app is marked forever).

**Admin overview**: a table of all open findings across apps (app, owner, rule, severity, since, evidence, action),
sorted by severity then age, plus the platform rows (`node.overcommit`, `app.stuck_error`). This is where findings
that an owner cannot act on (at the ceiling, quota raises, volume growth) get an action.

**Is "Apply" one click?** No: one button plus one confirm. Reasons: most actions roll the pod, and Kubecost, the only
tool surveyed with an apply button, also confirms. The confirm shows the exact change and its effect:

```
Raise memory limit 256 MB → 320 MB
Restarts the app (about 10 s of downtime with 1 replica).
[Apply] [Cancel]
```

Apply calls the existing endpoints (owner: `PATCH /apps/{id}` resources, subject to
`OWNER_RESOURCE_CHANGES_PER_HOUR`; admin: `PATCH /admin/apps/{id}/resources`, which already returns `restarts_app`;
rollback: `POST /apps/{id}/rollback`). No new write path, so validation, caps and audit stay in one place. The audit
detail should carry the rule id. Findings with no safe mechanical fix (crash loop, leak, failing health check) get a
link (logs, settings), not a button.

**Wording rules**: number, unit, window and limit in every line. Say what was measured ("at or above 90% of its
limit in 31 of 180 samples"), not a conclusion we cannot prove. Use the names in the UI (Memory, CPU, Scratch disk,
Database, Bucket, Volume). No "we recommend", no "consider"; state the change.

### 3.5 Phasing

**Phase 1: about one day** (backend-dev about 5 h, frontend-dev about 3 h, plus the usual review pass)

- `app/advisor.py` with the 16 phase 1 rules and guard rails; table-driven tests with fake facts.
- `GET /apps/{id}/advice` (member-scoped like the other app ops) and `GET /admin/advice` (all apps, batched queries).
- Advice card on the health page with action buttons wired to existing endpoints behind a confirm; admin table.
- No migration, no scheduler job, no cluster change, no new permission. Not in phase 1: dismiss, apps-table marker,
  "since" timestamps.
- If a day is tight, cut to the eight rules with the clearest action: `mem.oom`, `mem.near_limit`, `cpu.saturated`,
  `restart.loop`, `db.near_quota`, `bucket.near_quota`, `volume.near_full`, `health.failing`.

**Phase 2: 2 to 3 days**

- Migration: `app_metric_hourly` and `app_advice`. Hourly rollup job and a 15-min evaluate job in `app/scheduler.py`.
- The seven phase 2 rules; dismiss; apps-table marker; 14-day chart option on the health page.
- Needs 5 to 7 days of rollup data before the right-sizing rules speak. Ship the table early.

**Phase 3: per item, each needs a go-ahead**

- Throttling ground truth (RBAC `nodes/metrics`, kubelet reachability check, security review).
- Traefik counters for idle and error rate (verify the chart default first).
- DB connection signal.
- Notifications (email or webhook digest) with Coolify-style debounce. Out of scope until Chas wants push delivery.

### 3.6 Risks

- **Noise.** The fastest way to get the card ignored. Guard rails, `info` never marking the table, and dismiss are
  the controls. Start with conservative thresholds and tune on real apps.
- **Wrong advice from short history.** Phase 1 sees 3 hours. It must only make claims a 3-hour window supports
  (OOM, saturation, near quota), never "oversized".
- **Proxy mistaken for measurement.** `cpu.saturated` is not throttling data; the wording must stay honest until
  phase 3.
- **Raising limits on a 2 vCPU / 4 GB node.** Limits are overcommitted by design. Every admin "raise" should show
  node headroom next to the button, and `node.overcommit` should exist before raises become routine.
- **Volume growth costs money.** A grow suggestion is a paid change; it needs a request in `docs/requests/`, not a
  button.

---

## 4. Open questions for Chas

1. Audience: is lowering limits worth showing to owners at all, given it frees no node capacity and there is no
   per-app bill? Proposal: info-only for owners, counted for the admin.
2. Apply: button plus confirm for owners on their own app (within the ceiling). Agreed, or advice-only at first?
3. At the cap: should "ask an admin" stay text, or create a visible request in the admin overview?
4. Phase 3 go-aheads: (a) `nodes/metrics` RBAC for throttling ground truth, (b) Traefik metrics scrape for idle and
   error rate. Both are platform changes.
5. Idle apps: once measurable, what should happen after 30 idle days: admin list only, or a suggestion to the owner?
6. Push delivery (email or webhook), or in-app only?

---

## Sources

- [V1] VPA flags (recommender defaults): https://github.com/kubernetes/autoscaler/blob/master/vertical-pod-autoscaler/docs/flags.md
- [G1] Goldilocks FAQ: https://goldilocks.docs.fairwinds.com/faq/
- [K1] KRR README: https://github.com/robusta-dev/krr
- [C1] Kubecost container request right-sizing: https://www.ibm.com/docs/en/SSW0JQG_2.x/using-kubecost/navigating-the-kubecost-ui/savings/container-request-right-sizing-recommendations.html
- [O1] OpenCost specification: https://opencost.io/docs/specification
- [R1] Render notifications: https://render.com/docs/notifications
- [RW1] Railway, right-size CPU and memory: https://railway.com/guides/right-size-cpu-memory
- [RW2] Railway, alerts for crashes and failed deploys: https://docs.railway.com/guides/alerts-crashes-failed-deploys (read via search summary only)
- [F1] Fly.io metrics: https://docs.fly.io/monitoring/metrics
- [N1] Northflank infrastructure alerts: https://northflank.com/docs/v1/application/observe/set-infrastructure-alerts
- [CO1] Coolify notification events: https://coolify.io/docs/core/notifications/events
- [KB1] Kubernetes pod lifecycle (restart back-off): https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/
- [KA1] Kubelet authentication/authorization: https://kubernetes.io/docs/reference/access-authn-authz/kubelet-authn-authz/
- [E1] NIST handbook, EWMA control charts: https://www.itl.nist.gov/div898/handbook/pmc/section3/pmc324.htm
