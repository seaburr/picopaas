# PicoPaaS roadmap

Where PicoPaaS sits: a strict, multi-tenant mini-PaaS (the self-hosted Dokku/CapRover/Coolify space). These are
the next changes to make it more useful/flexible, chosen to use the infra we already run (DOKS, Traefik,
cert-manager, metrics-server, MySQL, GHCR) with no new paid resources.

## In progress / recently built

### Custom domains + automatic TLS — SHIPPED + e2e VERIFIED 2026-10-20
Verified end-to-end against a real external domain: `demo.seaburr.io` CNAME'd to the LB + ownership TXT → verify →
cert-manager issued a **trusted prod Let's Encrypt cert** (`CN=demo.seaburr.io`) first try, served over HTTPS (HTTP
301→HTTPS). The live e2e caught three gaps in running the ACME HTTP-01 solver inside a hardened tenant namespace,
all fixed (commit c2494cd): solver Service forced `ClusterIP` (tenant quota forbids NodePort); ResourceQuota grants
+1 Service / +1 Pod of headroom only when a custom domain is attached; a narrow `allow-acme-solver` NetworkPolicy
lets Traefik reach the solver pod on 8089.
Users attach their own domain (e.g. `blog.example.com`) to an app, in addition to the generated
`*.picopaas.xyz` subdomain. **Shipped shape:** `App.custom_domain*` fields + migration
`f3b8d6c4a1e9`; `app/custom_domains.py` (ownership-TXT + CNAME→LB verification via `dnspython`, behind a `Lookup`
interface); owner endpoints `PUT/POST verify/DELETE /apps/{id}/domain`; reconciler renders a second Ingress
(`app-<id>-custom`) annotated for cert-manager + tears it down when detached; a new **HTTP-01 ClusterIssuer**
(`deploy/cert-manager/clusterissuer-http01.yaml`, staging + prod) because the existing DNS-01 issuers are
DO-zone-only; UI on the app detail page. **Remaining:** apply the HTTP-01 issuer (staging first) and verify a real
cert issues end-to-end against a `seaburr.io` test subdomain (Chas adds the CNAME + ownership TXT). The original
design sketch and DNS-verification research follow (kept for reference):
- New `custom_domain` field on `App` (admin-approved, or self-serve with verification).
- The user creates a **CNAME** from their domain to the platform LB hostname (the `lb` DNS record / LB IP
  129.212.135.231). Apex domains can't CNAME — require a subdomain or an ALIAS/flattening provider.
- **Verify ownership** before provisioning: confirm the CNAME actually resolves to our LB (stops someone
  claiming a domain they don't control / domain-takeover hygiene).
- The reconciler renders an extra Ingress for the custom host and a cert-manager `Certificate` issued by
  Let's Encrypt via the **HTTP-01** solver through Traefik (both already deployed — no DNS-01, no new wildcard).
- Serve the app on both its generated subdomain and the verified custom host; the unknown-host styled-404 path
  must treat a verified custom domain as "known."
- No new infra/cost. Watch Let's Encrypt rate limits and cert-issuance failures.

#### DNS verification + not mis-issuing a TLS cert (security/network review, 2026-10-20)
Chas's question: the "special TXT record" other platforms (Vercel `_vercel`, Netlify, GitHub Pages, Cloudflare for
SaaS) ask you to add. There are **two different TXT records** people conflate, and we want to be clear which we use:

1. **Ownership pre-verification TXT** — *our* check, not the CA's. We tell the user to add
   `_picopaas-challenge.<their-host> TXT <random-token-we-generated-and-stored>`, then poll public DNS until it
   appears. This proves the user controls the zone **before** we create any routing or attempt issuance. This is the
   record most "add this TXT to verify your domain" flows are. It is independent of ACME.
2. **ACME DNS-01 TXT** — the *CA's* proof of control: `_acme-challenge.<host> TXT <per-issuance-token>`. Only needed
   if we issue via DNS-01 (required for wildcards; lets you issue before traffic cuts over). Changes every
   issuance/renewal.

**What actually prevents a bad/mis-issued cert, in order of importance:**
- **The ACME challenge is the real gate, not our TXT check.** With **HTTP-01** (our tabled choice) a cert can only be
  issued once the user's CNAME already points at our LB and Traefik serves the `/.well-known/acme-challenge/...`
  token — i.e. control is *implicit*. An attacker who doesn't route the domain to us cannot get us to mint a cert for
  it. So HTTP-01 alone already stops cross-tenant cert mis-issuance.
- **Add the ownership TXT (#1) anyway, as a gate on creating the route.** HTTP-01 proves routing, but we still don't
  want to create an Ingress / mark a host "known" for a domain the user merely typed. Verify TXT ownership first, then
  provision. This also stops a tenant from pre-claiming a host they don't yet control (first-claim hijack) and is our
  hook for **dangling-CNAME / subdomain-takeover** hygiene: re-verify periodically and tear the Ingress + Certificate
  down when the CNAME stops resolving to us or the app is deleted, so we never keep serving (or holding a cert for) a
  domain that has moved on.
- **CAA records:** the user's zone may publish a `CAA` that forbids Let's Encrypt — issuance will then fail no matter
  what. Surface this in the UI pre-flight ("add `0 issue \"letsencrypt.org\"` or remove the blocking CAA") instead of
  a silent cert-pending hang.
- **Apex domains can't CNAME** (RFC) — require a subdomain, or an ALIAS/flattening provider; don't try to issue for a
  bare apex pointed by A-record unless the user accepts they must re-point on LB-IP change.
- **Rate limits:** Let's Encrypt caps certs/registered-domain/week and has a failed-validation limit. A verify-before-
  issue flow (don't fire ACME until TXT + CNAME both resolve) keeps us well under the failure cap; use the LE
  **staging** environment while building.
- **If we ever need wildcards or issue-before-cutover**, switch that path to **DNS-01 with CNAME delegation**: the
  user CNAMEs `_acme-challenge.<host>` to a zone we control, so we never hold the user's DNS credentials and never
  need a full-access DNS API token (scoped-token principle). Not needed for the HTTP-01 per-host plan above.

Net for PicoPaaS: **ownership TXT (#1) to gate provisioning + HTTP-01 for issuance + periodic re-verification for
takeover hygiene + a CAA pre-flight.** No new infra. Sources:
[ACME challenge deep dive](https://www.namesilo.com/blog/en/domain-security/acme-challenges-deep-dive-http-01-vs-dns-01-vs-tls-alpn-01),
[BYO-domain auto-SSL / DNS-01 at scale](https://www.dchost.com/blog/en/bring-your-own-domain-get-auto-ssl-how-dns-01-acme-scales-multi-tenant-saas-without-drama/),
[custom domains for multi-tenant SaaS](https://www.dchost.com/blog/en/custom-domains-and-subdomains-for-multi-tenant-saas/).

### Terraform provider (`terraform-provider-picopaas`) — future, 2026-10-20
Chas: "we've done it before for Tribal, so the whole thing is understood." A provider wrapping the existing REST API
(`terraform-provider-picopaas`) would let users declare apps/env/DB/storage as HCL — the IaC story PaaS users expect.
- Resources: `picopaas_app` (image, port, env/secrets, want_db, want_storage, health check, auto_update),
  data sources for templates and app status; auth via an API key (ties into API scopes, below — a CI token should be
  able to deploy without full account control).
- Build model copies Tribal's: Go + terraform-plugin-framework, acceptance tests against a throwaway account, released
  to the Terraform Registry. Pure API client, **no new platform infra or cost.**
- Prereq worth doing first: a documented, versioned API surface (and the `/api/` prefix, issue #10) so the provider
  isn't pinned to SPA-shared paths.

## Shipped (2026-10-20 round)
2. App templates / one-click catalog (`/templates`: Ghost, nginx static site, podinfo) — DONE.
3. Instant rollback to a previous image digest (last 5 per app) — DONE.
4. Deploy health-gate + auto-rollback to the last good digest on a failed image rollout — DONE.
8. Per-app CPU/memory history on the app detail page — DONE.

## Backlog (free, common on other PaaS)
- **Scale-to-zero on idle (item 7, deferred 2026-10-20).** Sleeping idle apps to 0 replicas raises density
  (free capacity), but *transparent* wake-on-request needs an activator in the request path
  (Knative / KEDA http-add-on) = a new component, and auto-detecting idle needs a traffic signal we don't yet
  collect (Traefik access logs/metrics). Options when revisited: (a) manual Sleep/Wake (pause/resume) buttons —
  no infra, buildable anytime; (b) auto-sleep after idle + a platform "wake this app" page that scales to 1 and
  redirects (needs the traffic signal); (c) a real activator (new infra). Chas deferred the whole item for now.

## Backlog (free, common on other PaaS)
- Scheduled jobs (cron) per app — a k8s CronJob running the app image with a command.
- One-off run (`picopaas run <app> -- <cmd>`) — a short-lived Job with the app's env.
- Outbound deploy webhooks (Slack/Discord) — reuse the registry SSRF guard for the user URL.
- Build from a Dockerfile/source in-cluster (kaniko/BuildKit Job → GHCR). Highest-impact (removes the
  "bring a prebuilt public image" barrier) but the one item that adds real node CPU/disk at build time, so
  opt-in + quota-bounded, not strictly free.

## Persistent volumes (block storage): planned (Chas, 2026-10-22)
Today an app's only disk is its 250 MB scratch disk, which is wiped on every restart. Many self-hosted apps
expect a persistent folder: SQLite apps (Uptime Kuma, Vaultwarden), n8n's user folder (binary data, community
nodes, event log), and file uploads. Add an opt-in `resources.volume` that mounts one small block-storage
volume per app.
- **What DOKS already gives us (checked on the cluster):** the DigitalOcean block-storage CSI driver is installed,
  and `do-block-storage` is the default StorageClass. The driver attaches and detaches the volume when the pod moves
  to another node, so we don't write that logic. A node can attach up to 15 volumes, which is more than the
  ~8–12 apps a node runs.
- **Cost:** $0.10/GiB/month, billed while the volume exists, even when detached. The minimum size is 1 GiB, so
  about $0.10/month per app. Snapshots cost $0.06/GiB/month. A request in `docs/requests/` is needed before building.
- **What the platform has to handle:**
  - **One copy only:** volumes are ReadWriteOnce, so a volume app runs exactly one replica.
  - **Deploys:** they must use the `Recreate` strategy (a rolling update deadlocks on the attach), so each deploy
    has a short gap.
  - **Node drains:** about a minute of downtime while the volume moves.
  - **Permissions:** `fsGroup` so the non-root UID can write.
  - **Quotas:** a size cap per app, and a ResourceQuota on storage and PVC count.
  - **Deletion:** the app's volume goes with the app (reclaim `Delete`).
  - **Backups:** a snapshot policy.
  - **Sign-off:** a pentest pass.
- **Not a fix for n8n's database quota:** n8n's limit on PostgreSQL is the 50 MB database. A volume would fix its
  binary data and community nodes, not that.

## Managed PostgreSQL support: shipped 2026-10-22
Per-app PostgreSQL databases (`resources.db_engine: pg`) now run alongside MySQL on a managed `picopaas-pg-nyc3` cluster. Original design notes follow. Full feasibility, protection-by-protection
mapping, quota design, cost and the "ditch MySQL?" analysis: [docs/requests/0006-postgresql-support.md](requests/0006-postgresql-support.md).
- Every isolation protection (per-app DB+role, `REVOKE ... FROM PUBLIC`, TLS, egress netpol, connection cap, rotation) maps
  cleanly to Postgres. The **50 MB size quota is the hard part**: no native cap and the tenant role owns its objects, so
  the recommended design is a two-stage hybrid (read-only default + session kill, then CONNECT lockout), weaker than
  MySQL's REVOKE against a hostile tenant. The `sql_require_primary_key` (Wiki.js) blocker disappears.
- Recommended path: in-cluster pilot (about $0-2/mo), then DO Managed PostgreSQL (about $15/mo, needs a budget request) if
  adopted. Run alongside MySQL; revisit retiring MySQL later (Ghost is MySQL-only and the control plane uses MySQL).
- Unlocks n8n, Listmonk, Miniflux, Wiki.js and more. Status: shipped (2026-10-22).

## Notes from the 2026-10-20 Ghost load test
On the old **100m CPU / 384 MB**, Ghost (Node SSR) was **CPU-bound**: a single concurrent reader already
pinned the 100m cap (~1 req/s, ~640 ms p50); ~5 concurrent started timing out. Memory stayed ~150 MB of 384.
Actions taken:
- **Default CPU limit raised 100m → 200m (Chas, 2026-10-20)** to rebalance for Node SSR; request stays 64m so
  scheduling density is unchanged. Per-app override still goes up to 1000m for sustained traffic.
- Documented "bump CPU for sustained traffic" (templates note, USER_GUIDE).
- **Caching not added (deferred).** A user who wants it can run their own DNS through Cloudflare and get CDN/edge
  caching in front of their app — no platform work needed. A built-in cache would be the bigger lever but is a
  separate feature.
