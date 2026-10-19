# Request 0001: Core infrastructure (status: AWAITING CHAS)

Nothing gets provisioned until Chas approves. Terraform for this lives in `infra/` once written.

## 1. What is being deployed
- DOKS cluster (non-HA control plane) with 1 × `s-2vcpu-4gb` node, region `nyc3`
- Spaces subscription: one private bucket for Terraform state + MySQL backups, plus one bucket per app on demand
- 10 GiB block volume for in-cluster MySQL
- DNS: wildcard `*.seaburr.io` A record (plus `picopaas.seaburr.io` for the platform UI)
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
- [ ] Approve the plan above (or choose the managed option, ~$56/mo, see ARCHITECTURE.md §1)
- [ ] Create a DO API token (read/write) and add it as GitHub repo secret `DIGITALOCEAN_TOKEN`
- [ ] Create a Spaces access key (full access, used by the platform to create per-app buckets/keys) and add
      it as repo secrets `SPACES_ACCESS_KEY_ID` / `SPACES_SECRET_ACCESS_KEY`
- [ ] Answer the open questions in ARCHITECTURE.md §3
