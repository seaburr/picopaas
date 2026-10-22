# PicoPaaS status page (status.picopaas.xyz)

Runs [Gatus](https://github.com/TwiN/gatus) `v5.37.0` as an ordinary PicoPaaS tenant app. Gatus polls our
endpoints every 1-5 minutes, evaluates conditions, and renders the public dashboard. Config is baked into a thin
image because apps have no config-file mount.

## Decision (OSS evaluation)
- **Gatus: chosen.** One static Go binary, YAML config, built-in poller and dashboard, in-memory storage option, tiny
  footprint (well inside 384MB / 0.2 core), image is `FROM scratch` (a few tens of MB, far under the 512MB cap).
- Uptime Kuma: needs a persistent SQLite volume (none exists) and is heavier. Rejected.
- Cachet: PHP + DB, manual incidents, no native polling. Rejected.
- Statping-ng: unmaintained. Rejected.
- Upptime/cstate: run on GitHub Actions/static hosting, not on our platform, and the point is to run on PicoPaaS. Rejected.

## Egress finding (verified from code)
`app/k8s_manifests.py::network_policies` gives every tenant namespace default-deny plus `allow-egress-dns` (kube-system:53)
and `allow-egress-internet`: `0.0.0.0/0` except RFC1918/link-local (10/8, 172.16/12, 192.168/16, 169.254/16), all TCP
except SMTP 25/465/587, all UDP. `platform.picopaas.xyz` resolves to the public LB IP, so a tenant app **can** probe it
and the public internet. In-cluster hairpin is handled: `deploy/traefik` publishes the LB by hostname so kube-proxy does not
short-circuit pods hitting the LB IP. Consequences: email alerting is impossible (SMTP blocked), use a webhook provider;
probes cannot reach private/cluster-internal addresses, so this page only sees what a customer sees (which is the point).
**Recommendation: run as a tenant app** (no new platform privileges, no NetworkPolicy exceptions, dogfoods the platform).
Caveat: if the platform or cluster is down, the status page is down with it. Acceptable for a throwaway PaaS; if an
independent view is wanted later, host it elsewhere. NOT verified: a live probe from inside a pod (the first deploy is that test).

## Trade-offs
- Storage is `memory`: check history resets on every pod restart/redeploy (PicoPaaS has no persistent volume).
- The platform `/healthz` is shallow (`{"status":"ok"}` from the API process). Deeper health (DB, reconciler, nodes) is behind
  admin auth (`/admin/health`) and is deliberately not exposed publicly.
- The app health check on PicoPaaS can use Gatus's own `/health` path (`GET /health` returns `{"status":"UP"}`).

## Files
- `Dockerfile`: pinned `twinproduction/gatus:v5.37.0`, `USER 10001:10001` (upstream runs as root; PicoPaaS rejects root), copies config.
- `config.yaml`: endpoints, UI, optional alerting (commented out).

## Local test (Docker required; not run by the author, the daemon was down)
```bash
cd deploy/status
docker build -t picopaas-status:local .
docker run --rm -p 8080:8080 picopaas-status:local   # open http://localhost:8080
docker inspect picopaas-status:local --format '{{.Config.User}}'   # expect 10001:10001
```

## Go live
Load credentials without printing them: `source ~/.picopaas-e2e.env` (provides `E2E_ADMIN_TOKEN`). Never echo it.

1. Build and push via CI (no local Docker needed). The `.github/workflows/status-image.yml` workflow builds
   `deploy/status` and pushes `ghcr.io/seaburr/picopaas-status:5.37.0` (and a short-SHA tag) on any push to `main`
   that touches `deploy/status/**`. Confirm the run is green (`gh run list --workflow status-image.yml`).
   **One-time:** make the GHCR package **public** so the platform can pull it anonymously
   (GitHub > seaburr > Packages > picopaas-status > Package settings > Change visibility > Public).
   Local build is only a fallback and needs a GHCR login with `write:packages`:
   ```bash
   cd deploy/status && docker buildx build --platform linux/amd64 -t ghcr.io/seaburr/picopaas-status:5.37.0 --push .
   ```
2. Create the app (admin account, so the per-account app cap and limit exemptions apply as for the blog):
   ```bash
   curl -sS -X POST https://platform.picopaas.xyz/apps \
     -H "Authorization: Bearer $E2E_ADMIN_TOKEN" -H "Content-Type: application/json" \
     -d '{"name":"picopaas-status","image":"ghcr.io/seaburr/picopaas-status:5.37.0","port":8080,
          "want_db":false,"want_storage":false,"env":[],
          "health_check_enabled":true,"health_check_path":"/health","auto_update":false}'
   ```
   Note the returned `id` as `APP_ID`. Wait until `GET /apps/$APP_ID` shows status running (reconciler, ~1 minute).
3. Reserve the subdomain (platform admin only):
   ```bash
   curl -sS -X PUT https://platform.picopaas.xyz/admin/apps/$APP_ID/platform-host \
     -H "Authorization: Bearer $E2E_ADMIN_TOKEN" -H "Content-Type: application/json" \
     -d '{"subdomain":"status"}'
   ```
   Covered by the existing `*.picopaas.xyz` wildcard cert and DNS; no new records or paid resources. 409 means taken; 422 means reserved/invalid label.
4. Verify: `curl -s https://status.picopaas.xyz/health` returns `{"status":"UP"}`; the dashboard at
   https://status.picopaas.xyz shows all endpoints green after a minute or two. If "API health" is red from inside the cluster,
   check egress/hairpin (see above).

## Updating config
Edit `config.yaml`, bump the image tag in `.github/workflows/status-image.yml` (e.g. `5.37.0-r2`), push to `main`
(CI rebuilds and pushes), then `PATCH /apps/$APP_ID` with the new `image`. Bumping Gatus: change the `FROM` pin in
the Dockerfile (use a release at least 2 weeks old) and the workflow tag.

## Enabling alerting (optional)
SMTP is blocked for tenants, so use a webhook. Uncomment the `alerting:` block in `config.yaml`, add `alerts:` to endpoints, e.g.
```yaml
    alerts:
      - type: discord
```
Set the secret as an app env var (marked secret) rather than in the image:
```bash
curl -sS -X PATCH https://platform.picopaas.xyz/apps/$APP_ID -H "Authorization: Bearer $E2E_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"env":[{"key":"DISCORD_WEBHOOK_URL","value":"<placeholder: your webhook URL>","is_secret":true}]}'
```
Check `docs/API.md` for the exact env var field names. PATCH `env` replaces the list, so include existing vars. Rebuild/push the image
after editing config. Rollback: delete the platform-host (`DELETE /admin/apps/$APP_ID/platform-host`) and/or the app.
