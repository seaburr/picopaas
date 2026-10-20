# PicoPaaS roadmap

Where PicoPaaS sits: a strict, multi-tenant mini-PaaS (the self-hosted Dokku/CapRover/Coolify space). These are
the next changes to make it more useful/flexible, chosen to use the infra we already run (DOKS, Traefik,
cert-manager, metrics-server, MySQL, GHCR) with no new paid resources.

## Tabled (approved direction, build later)

### Custom domains + automatic TLS  — TABLED by Chas 2026-10-20
Let a user attach their own domain (e.g. `blog.example.com`) to an app, in addition to the generated
`*.raisehellpraisedale.xyz` subdomain. Design sketch for when we build it:
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
- One-off run (`picopaas run <app> -- <cmd>`) — a short-lived Job with the app's env, à la `heroku run`.
- Outbound deploy webhooks (Slack/Discord) — reuse the registry SSRF guard for the user URL.
- Build from a Dockerfile/source in-cluster (kaniko/BuildKit Job → GHCR). Highest-impact (removes the
  "bring a prebuilt public image" barrier) but the one item that adds real node CPU/disk at build time, so
  opt-in + quota-bounded, not strictly free.

## Notes from the 2026-10-20 Ghost load test
On the default **100m CPU / 384 MB**, Ghost (Node SSR) is **CPU-bound**: a single concurrent reader already
pins the 100m cap (~1 req/s, ~640 ms p50); ~5 concurrent starts timing out. Memory stayed ~150 MB of 384.
Implications:
- The useful lever for CMS-class apps is **CPU**, via the existing per-app resource override (up to 1000m).
- The default caps are unbalanced for Node SSR (generous RAM, tight CPU); consider rebalancing or documenting
  "bump CPU for sustained traffic."
- A cache/CDN in front would raise effective reader count far more than RAM.
