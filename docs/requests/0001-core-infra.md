# Request 0001: Core infrastructure (status: APPROVED 2026-10-19: NYC3, managed MySQL Premium AMD + LB + s-2vcpu-4gb node, ≈ $60.30/mo)

## 2026-10-18 update from Chas
- He's leaning toward the **managed** option (managed MySQL + LB) for reliability, which costs more than the lean plan. Not final yet.
- Region: **ATL1 preferred**, with NYC3 as the fallback. ATL1 has DOKS, Spaces, LB, volumes, and DOCR, but **managed MySQL in ATL1 is
  Premium Intel only**, which costs more than the $15.15 basic 1 GiB plan. Price TBD. Check the control panel when ordering.
- DNS: wildcards OK. Domain superseded 2026-10-19: apps and platform use `raisehellpraisedale.xyz` (see request 0003).
- Tokens arrive 2026-10-19.

### Managed option in ATL1 (estimate)
| | Monthly |
|---|---|
| Node s-2vcpu-4gb $24 in NYC3 (ATL1 had no DOKS capacity for any size; Chas 2026-10-19) + LB $12 + Spaces $5 + managed MySQL Premium AMD 1 GiB $19.30 (`db-amd-1vcpu-1gb`, Chas 2026-10-19) | **~$60.30, over the $40 budget by ~$20**; autoscaled 2nd node +$24 |
| Per deployed app | same as lean: ~$0 for the first ~8–10 apps, then ~$2/app |
Middle ground: managed MySQL with no LB (~$49), or an LB with in-cluster MySQL (~$42). Chas decides.
The code is identical in every variant. Only Terraform flags differ (`use_managed_mysql`, `use_load_balancer`).


Nothing gets provisioned until Chas approves. Terraform for this lives in `infra/` once written.

## 1. What is being deployed
- DOKS cluster (non-HA control plane) with 1 × `s-2vcpu-4gb` node, region `atl1` (fallback `nyc3`)
- Spaces subscription: one private platform bucket (MySQL backups; Terraform state lives in Terraform Cloud), plus one bucket per app on demand
- 10 GiB block volume for in-cluster MySQL
- DNS: wildcard `*.raisehellpraisedale.xyz` A record (plus `picopaas.raisehellpraisedale.xyz` for the platform UI)
- No load balancer. Ingress runs on node hostPorts.

## 2. Why
This is the smallest footprint that can run the platform, its MySQL, and the first several user apps
with DB + storage while staying under the $40/mo budget.

## 3. Cost
| | Monthly |
|---|---|
| Static: node $24 + Spaces $5 + volume $1 + control plane $0 | **~$30** |
| Per deployed app | **~$0** for the first ~8–10 apps, then about **$2/app** (one extra $24 node per ~12 apps) |

## What we need from Chas (when ready, not yet)
- [ ] Domain move: see `0003-app-domain.md` checklist (nameservers to DO, add domain in DO panel)
- [ ] Approve the plan above (or choose the managed option, ~$56/mo, see ARCHITECTURE.md §1)
- [ ] Create a DO API token (read/write) and add it as GitHub repo secret `DIGITALOCEAN_TOKEN`
- [ ] Create a Spaces access key (full access, used by the platform to create per-app buckets/keys) and add
      it as repo secrets `SPACES_ACCESS_KEY_ID` / `SPACES_SECRET_ACCESS_KEY`
- [ ] Create the Terraform Cloud workspace `picopaas` in org `seaburr`. Add `DIGITALOCEAN_TOKEN`, `SPACES_ACCESS_KEY_ID`,
      `SPACES_SECRET_ACCESS_KEY` as sensitive workspace env vars, and add a TFC API token as GitHub repo secret `TF_API_TOKEN`
- [ ] Make the GHCR package `ghcr.io/seaburr/picopaas` public (new packages start private, so the cluster can't pull it), or tell us to use a pull secret
- [ ] Approve autoscaler ceiling `max_nodes = 2` (worst case +$24/mo while the extra node is up)
- [ ] Pick a final variant: lean / LB-only / managed-DB-only / both (see table above)
