# Request 0004: move to the picopaas.xyz domain

Status: PENDING (cutover runs via CI after the repo changes merge). No new paid resource beyond the domain Chas already bought.

## 1) What is being deployed
Repo-wide switch from `raisehellpraisedale.xyz` to `picopaas.xyz`: platform at `platform.picopaas.xyz`, apex landing page at
`picopaas.xyz`, apps at `<label>.picopaas.xyz`, a `*.picopaas.xyz` wildcard cert (secret `wildcard-picopaas-xyz-tls`), and
`LEGACY_HOSTS` now empty by default (the old `picopaas.<domain>` redirect host is dropped). `App.hostname` stores only the
label, so existing apps re-point automatically; there is no data migration.

## 2) Why
A proper product domain instead of the throwaway one.

## 3) Cost
Static: $0/mo extra (DO DNS is free; the domain itself was already purchased). Per app deployed: $0.

## Preconditions for Chas
- [ ] In DO: Networking > Domains > add `picopaas.xyz` (setting DO nameservers at the registrar is not enough; bootstrap runs
      `doctl compute domain records list picopaas.xyz` and fails otherwise).
- [ ] Confirm the `DIGITALOCEAN_TOKEN` GitHub secret is valid (and has DNS write scope for the new domain).
- [ ] Keep `PICOPAAS_FERNET_KEY` unchanged so stored secrets keep decrypting.

## Cutover
1. Merge the repo changes.
2. Run the `bootstrap.yml` workflow. It upserts A records `*`, `@`, `platform`, `lb` under `picopaas.xyz`, sets the `APP_DOMAIN`
   secret to `picopaas.xyz`, (re)issues the `*.picopaas.xyz` wildcard cert via cert-manager DNS-01, and rolls the platform.
   The health check waits for the cert (~10 min).
3. If the in-cluster `dns-sync` is deployed, re-apply `deploy/dns-sync/dns-sync.yaml` (DOMAIN now `picopaas.xyz`).

## Post-cutover
- Existing apps auto-serve under `*.picopaas.xyz` (hostname label unchanged).
- Set the Ghost blog app's reserved subdomain so it serves at `blog.picopaas.xyz` (covered by the `*` A record and wildcard cert):
  `PUT /admin/apps/{id}/platform-host` with `{"subdomain":"blog"}`.

## Notes / caveats
- The old `raisehellpraisedale.xyz` zone is no longer managed; its wildcard cert will lapse. To keep a redirect later, set the
  `LEGACY_HOSTS` env var (comma-separated) on the platform.
- **Flag for Chas:** the live demo app's custom domain `demo.seaburr.io` CNAMEs to `lb.raisehellpraisedale.xyz`. To keep it
  working, either keep the old `lb` A record pointing at the LB IP, or re-point `demo.seaburr.io` to `lb.picopaas.xyz`
  (the platform's `CUSTOM_DOMAIN_CNAME_TARGET` now derives to `lb.picopaas.xyz`, so verification will expect that target).
- Old bookmarks/CLI configs pointing at `platform.raisehellpraisedale.xyz` stop working; users set `PICOPAAS_URL` or upgrade the CLI.
