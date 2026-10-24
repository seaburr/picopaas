# Porting PicoPaaS to Azure (AKS + Flexible Server + Blob Storage)

Research only (2026-10-23, [infra-ops]). Nothing was provisioned or applied; no Azure or DigitalOcean account was touched.
Question from Chas: what would it take to run PicoPaaS on AKS, Azure Database for MySQL/PostgreSQL Flexible Server and Azure
Blob Storage, and can it stay inside the current ~$60-75/mo window?

**Short answer.** The code port is about 25 agent-days, most of it the object store (Blob is not S3) and the infrastructure
and workflows rewrite. On AKS the monthly bill goes **up**, from $75.30 to roughly $105 (cheapest viable) to $125 (like for
like), mainly because AKS system node pools cannot use cheap B-series VMs and the AKS Standard Load Balancer is mandatory.
A variant that skips AKS (one Azure VM running k3s, managed MySQL/PostgreSQL, Blob) lands at about $52-68 and does beat DO,
at the price of running the control plane ourselves.

## How to read the prices

- **VERIFIED** = pulled from the Azure Retail Prices API (https://prices.azure.com/api/retail/prices, region `eastus2`, USD,
  list price, pay-as-you-go, queried 2026-10-23) or an Azure Learn / pricing page fetched the same day. Monthly = hourly x 730 h.
- **EST** = estimated or recalled from Azure documentation without a fetch in this session. These need a spike before anyone
  relies on them. Every EST is listed again in section 4.
- DO figures are the ones in `docs/ARCHITECTURE.md` section 1 (checked 2026-10-20). Region: East US 2 (the closest equivalent to NYC3).

---

## 1. Service mapping (DO to Azure)

| Concern | Today (DO) | Azure equivalent | Price (East US 2) | Notes |
|---|---|---|---|---|
| Kubernetes control plane | DOKS non-HA, $0 | AKS **Free tier** $0, or Standard tier $0.10/h = $73/mo (VERIFIED, `Standard Uptime SLA` meter) | Free: $0 | Free = no financially backed SLA, "recommended fewer than 10 nodes", up to 1,000 nodes (VERIFIED, [AKS tiers](https://learn.microsoft.com/en-us/azure/aks/free-standard-pricing-tiers)). Matches our "hobby platform" stance. Standard is not worth $73 here. |
| Worker node | 1 x `s-2vcpu-4gb-amd`, $28 | **System pool cannot be B-series.** Cheapest non-B 2 vCPU / 4 GiB: `D2als_v6` $0.0804/h = **$58.69**; `D2as_v5` (8 GiB) $0.086/h = $62.78 | see left | VERIFIED: AKS "doesn't support B series VMs for system node pools" and a system pool needs at least 2 vCPU / 4 GB ([quotas page](https://learn.microsoft.com/en-us/azure/aks/quotas-skus-regions)); the [system pools page](https://learn.microsoft.com/en-us/azure/aks/use-system-pools) says 4 vCPU and "at least two nodes" for a pool, which contradicts the quotas page. Treat 1 node of 2 vCPU as the best case and spike it. A cluster must have a system pool, and apps may run on it (discouraged, allowed). |
| Burstable user pool | n/a | `B2s` (2 vCPU / 4 GiB) $0.0416/h = $30.37; `B2ls_v2` same price; `B2as_v2` (8 GiB) $0.0752/h = $54.90; `B2ms` (8 GiB) $0.0832/h = $60.74 | | VERIFIED prices. B-series is allowed on user pools only. Using one means paying for a system node as well, so it does not help unless the system pool is tiny. B2s baseline is 40% of 2 vCPU, so sustained tenant load drains credits (EST). |
| Spot tenant pool | n/a | Spot VMs (user pools only). Indicative `D2as_v5` spot $0.0159/h = ~$11.60/mo | | Retail API shows the spot figure but spot is variable and evictable (VERIFIED that a price is listed, indicative only). Needs a normal system node anyway ($58.69), so total compute rises. Not recommended. |
| Node OS disk | included | AKS defaults to a 128 GiB managed OS disk (EST). Premium P10 $17.92/mo, P4 (32 GiB) $4.80/mo (VERIFIED prices) | $4.80 with `os_disk_size_gb=32` | Easy to forget; set it in Terraform. |
| Load balancer | DO LB `lb-small`, $12 flat, no bandwidth charge | **Azure Standard Load Balancer**: first 5 rules $0.025/h = **$18.25/mo**, plus $0.005/GB data processed (VERIFIED) | $18.25 + $0.005/GB | AKS creates a Standard LB for node egress even if ingress is hostPort-only (EST), so "skip the LB with hostPort" saves nothing. |
| Public IPs | included with LB | Standard static IPv4 $0.005/h = **$3.65/mo** each (VERIFIED) | 2 x $3.65 = $7.30 | One for the Traefik Service, one for managed outbound (EST). Possibly one if the outbound frontend is shared (spike). |
| Managed MySQL | `db-s-1vcpu-1gb` $15.15 (10 GiB disk) | **MySQL Flexible Server Burstable B1ms** (1 vCore, 2 GiB) $0.017/h = **$12.41/mo**; storage $0.115/GB/mo (VERIFIED); minimum storage 20 GiB = $2.30 (VERIFIED by Learn training module) | **$14.71** | Backups free up to 100% of provisioned storage (VERIFIED, [pricing page](https://azure.microsoft.com/pricing/details/mysql)). Default `max_connections`: B1s 85, **B1ms 171**, B2s 341 (VERIFIED, [server parameters](https://learn.microsoft.com/en-us/azure/mysql/flexible-server/concepts-server-parameters)). DO gives about 76 total. TLS default on. |
| Managed PostgreSQL | `db-s-1vcpu-1gb` $15.15 | **PostgreSQL Flexible Server Burstable B1ms** $0.017/h = **$12.41/mo**; storage $0.115/GB/mo (VERIFIED); minimum storage 32 GiB (EST) = $3.68 | **$16.09** | B1ms: 50 connections, **35 usable** (VERIFIED, [limits](https://learn.microsoft.com/en-us/azure/postgresql/flexible-server/concepts-limits)). DO gives about 22 usable. Burstable has no built-in PgBouncer (VERIFIED, same page). B2s: 429 connections, $0.068/h = $49.64. |
| Object storage | Spaces $5 flat (250 GiB, 1 TiB egress) | **Blob Storage, Hot LRS**: $0.0184/GB/mo (first 50 TB), writes $0.05/10k, reads $0.004/10k, list/create-container $0.05/10k (VERIFIED, general-purpose v2) | ~$0.50/mo at our scale | No monthly minimum. Usage-based, so the platform drops from $5 to about $0.50. Does not speak S3 (section 3.1). |
| DNS | DO DNS $0 | Azure DNS public zone: $0.50/zone/mo + $0.40 per million queries (EST, the pricing page renders blanks) | ~$0.50 | **Recommendation: leave DNS on DO.** The domain's nameservers are already there, cert-manager has an in-tree DO DNS-01 solver, and `bootstrap` only upserts two A records. Costs $0 and removes a migration step. |
| Block volumes (PVC) | `do-block-storage`, $0.10/GiB, min 1 GiB, 15 attached per node | Azure Disk CSI. AKS ships `managed-csi` (StandardSSD_LRS) and `managed-csi-premium` (EST). Standard SSD bills by tier: E1 4 GiB $0.30, E3 16 GiB $1.20, E4 32 GiB $2.40, E10 128 GiB $9.60 (VERIFIED, ~$0.075/GiB, rounded up to the tier) | 1 GiB request costs $0.30 | Per-VM data-disk limit is **4** for 2 vCPU VMs such as D2as_v5 and B2s (third-party VM size table, not Learn; verify), vs 15 on DOKS. `managed-csi` is reclaimPolicy Delete, WaitForFirstConsumer, expansion allowed (EST). |
| Container registry | GHCR | GHCR unchanged | $0 | Anonymous pulls from GHCR work from any cluster. |
| Internet egress | Droplet transfer allowance pooled (size not verified here), overage $0.01/GiB | First **100 GB/mo free** (subscription-wide), then **$0.087/GB** (North America, VERIFIED, [bandwidth page](https://azure.microsoft.com/en-us/pricing/details/bandwidth/)); inbound free; same-region free | | See risk R3: this is the largest new unbounded cost. |
| State | Terraform Cloud | TFC keeps working with the `azurerm` provider; or an `azurerm` backend in a storage container (~$0.01/mo) | $0 | |
| CI credentials | long-lived `DIGITALOCEAN_TOKEN` GitHub secret | GitHub Actions OIDC federated credential on an Entra app / user-assigned managed identity (no secret stored) | $0 | Security improvement. |
| Monitoring | none | Leave Container Insights, Defender and Azure Monitor off. The portal pre-ticks some of them and Log Analytics bills per GB (EST). | $0 | |

---

## 2. Cost model

All figures USD per month, East US 2, 730 h, 2026-10-23. "Like for like" = the same shape as today (one app node, managed MySQL,
managed PostgreSQL, public LB, object storage). Compute row = one node that runs both system components and tenant apps.

| Line | DO today | Azure A: like for like (AKS Free) | Azure B: cheapest viable AKS | Azure C: VM + k3s, managed DBs | Azure D: VM + k3s, in-cluster MySQL, no PG |
|---|---|---|---|---|---|
| Control plane | $0 | $0 | $0 | $0 (we run k3s) | $0 |
| Node | $28.00 | $58.69 (D2als_v6) | $58.69 | $30.37 (B2ls_v2 / B2s) | $30.37 |
| Node OS disk | incl. | $4.80 (P4 32 GiB) | $4.80 | $2.40 (E4 32 GiB, Standard SSD) | $2.40 |
| Load balancer | $12.00 | $18.25 | $18.25 | $0 | $0 |
| Public IPs | incl. | $7.30 (2) | $7.30 (assume 2; maybe 1) | $3.65 (1, on the VM) | $3.65 |
| MySQL | $15.15 | $14.71 | $14.71 | $14.71 | $0 (StatefulSet, 8 GiB E2 disk $0.60) |
| PostgreSQL | $15.15 | $16.09 | $0 (PostgreSQL apps disabled) | $16.09 (or $0 if dropped) | $0 |
| Object storage | $5.00 | ~$0.50 | ~$0.50 | ~$0.50 | ~$0.50 |
| DNS | $0 | $0 (stay on DO) | $0 | $0 | $0 |
| **Total** | **$75.30** | **~$120.80** | **~$104.30** | **~$67.70** ($51.60 without PG) | **~$37.50** |
| vs DO | | **+$45.50 (+60%)** | **+$29.00 (+39%)** | **-$7.60 (-10%)** ($51.60: -31%) | **-$37.80** |

Notes on the columns.

- **A** with the 8 GiB `D2as_v5` instead is about $124.90, and roughly doubles app capacity per node, which is the better value per
  tenant if the budget moves up. If the docs' "system pool needs 2 nodes" is enforced, A doubles the node line to about $180.
- **B** gives up: managed PostgreSQL (the PostgreSQL feature stays off or runs in-cluster), and it still pays the $18.25 LB
  because AKS needs it for node egress. There is no hostPort trick here as there was on DO: the LB is billed whether or not
  Traefik uses it (EST; check with a scratch cluster).
- **C** is "Azure VM + k3s". What we give up: AKS-managed control plane, node image upgrades, the Azure CNI/Cilium integration,
  and the out-of-the-box Azure Disk CSI. We take on k3s upgrades, node patching, and node loss recovery (one VM, no
  autoscaler, same as DO's single node today). k3s embeds kube-router network policy, which supports `ipBlock` with `except`;
  the egress-bandwidth annotation needs the bandwidth CNI plugin (EST: not in k3s's default flannel setup, would need a spike
  or Cilium). The platform only needs a Kubernetes API, so Terraform becomes VM + cloud-init, and our existing `deploy/`
  manifests mostly carry over. B2-series baseline CPU credit limits apply (EST). Disk CSI would need the Azure cloud provider
  config on a self-built cluster; `local-path` volumes would be node-bound and not survive VM loss.
- **D** reuses the in-cluster MySQL manifests already in `deploy/mysql/`. It matches the original $40 target, but loses managed
  backups and failover and puts the platform DB on a single 8 GiB disk. Only for a pure throwaway stage.
- Free offers: Azure free accounts include 12 months of 750 h B1ms MySQL and PostgreSQL Flexible Server plus 32 GB storage, a B-series VM and
  small amounts of Blob and a $200 first-30-day credit (VERIFIED for MySQL via the [overview page](https://learn.microsoft.com/azure/mysql/flexible-server/overview);
  the rest from memory). **Not counted** in any column above.
- If the subscription is sponsored (for example Visual Studio or employer credits, amount unknown), A may cost Chas nothing. That is a question for him, not an assumption.

### Per-tenant marginal cost

| Resource | DO | Azure (A/B) | Comment |
|---|---|---|---|
| App compute | ~$2.50-3.50 (node $28 per 8-12 apps) | ~$5-7 ($58.69 per 8-12 apps on a 4 GiB node; AKS reserves more memory than DOKS, EST) or ~$3-4 on 8 GiB `D2as_v5` | Same shape, node is dearer. |
| MySQL DB (50 MB) | ~$0 (25 DBs per server) | ~$0 until the server's storage is exceeded: 100 tenants x 50 MB = 5 GB < 20 GiB minimum. Connections: 171 default / 10 per app leaves about 14-16 tenants at the per-app cap of 10 (raise `max_connections` up to 341 or lower the cap) | Better than DO's ~76 total connections. |
| PostgreSQL DB (50 MB) | ~$0, limited to about 4 apps by connections | ~$0, limited to about **7** apps by connections (35 usable / 5 per role); a B2s ($49.64) lifts that to ~80 | |
| Blob (100 MB) | $0 (inside $5) | 100 MB x $0.0184 = $0.002 storage. The 5-minute quota poll lists the container: 8,640 list ops/app/mo x $0.05/10k = **$0.043/app/mo** | Fine. Lengthen the poll or use a blob inventory if tenants multiply. |
| Volume (1 GiB request) | $0.10 | **$0.30** (billed as the 4 GiB tier); 10 GiB admin override bills the 16 GiB tier = $1.20 | At most 4 attached per node. |
| Egress | pooled allowance | $0.087/GB past 100 GB/mo | See R3. |

---

## 3. Code and platform changes, in order

Effort: S = under a day, M = 1-3 days, L = 3+ days (agent-days).

### 3.0 Seam refactor with DO still running (M, 2d). Do this first, it pays off even if Azure never happens

How clean is the seam today? Better than expected.

| Area | Verdict |
|---|---|
| `KubeProvider` (`app/providers/kube.py`) | Cloud-neutral (plain Kubernetes API, metrics.k8s.io). No change. |
| `DbAdmin` (`mysql.py`, `postgres.py`, `factory.py`) | Neutral SQL. DO-isms are constants: `ADMIN_DB = "defaultdb"`, port 25060 default (`scheduler.py:294`), the CA handling. Small config split. |
| `Registry` (GHCR) | Neutral. |
| `ObjectStore` interface | **The interface is Spaces-shaped** (`create_bucket_and_key`, `rotate_key`, `revoke_other_keys`, key-swap on `set_read_only`), and the callers are hard-wired to `SpacesStore`: `scheduler.py:71-76`, `routers/apps.py:356`, `routers/auth.py:136` (feature flag reads `DIGITALOCEAN_TOKEN`/`SPACES_*`). Needs a factory like `db_admin_for`. |
| Storage refs | `reconciler._storage_refs` + `env_refs.py` + `AppStorage` columns (`endpoint, region, bucket, access_key, secret_key_encrypted`) assume S3 semantics. |
| `quota.py` | Quota logic is store-agnostic except for the "Spaces grants are immutable, swap the key" path (lines 98-146). With SAS and stored access policies read-only becomes an in-place update, so that path simply returns `None`. |
| `k8s_manifests.py` | Neutral. DO touches: `PRIVATE_CIDRS` comment, `allow-egress-storage` built from private IPs (empty on Azure public Blob endpoints), `kubernetes.io/egress-bandwidth` annotation (Cilium bandwidth manager on DOKS). |
| `config.py` | `VOLUME_STORAGE_CLASS=do-block-storage`, "15 volumes per node" comment on `VOLUMES_MAX_TOTAL`, `SPACES_REGION`, `CUSTOM_DOMAIN_CNAME_TARGET=lb.<domain>`. |
| `db_host_metrics.py` / `health.py` | DO Prometheus endpoint (already off by default). An Azure Monitor equivalent is optional. |
| `deploy/` | `traefik/values.yaml` has `do-loadbalancer-*` annotations, `dns-sync` uses the DO API, cert-manager issuer is the DO solver. |
| `.github/workflows/bootstrap.yml` | Heavy `doctl` use: kubeconfig, LB IP lookup, DNS A records, DB connection details, DB CA fetch, VPC CIDR. This is the largest rewrite. |

Proposal: `CLOUD=do|azure` (default `do`) in `app/config.py`, which selects defaults and factories only. Concretely:

- `app/providers/factory.py` gains `object_store_for(cloud)` returning `SpacesStore` or `AzureBlobStore`.
- Introduce a provider-neutral `StorageCreds` shape: `{"kind": "s3"|"azure", ...}` so `storage.*` refs can differ by kind.
- Move cloud defaults into one table: storage class, per-node volume cap, DB admin database name, default DB port, CA bundle source, "provider configured" check.
- Keep `FakeObjectStore` as the contract test for both implementations.

### 3.1 Object storage: the biggest product decision (L, 5-8d, plus Chas decision)

**Blob Storage has no S3 API.** Confirmed by search 2026-10-23: no native S3 compatibility; only third-party layers (s3proxy,
Flexify.IO, a dated MinIO gateway). Today apps receive `storage.endpoint/region/bucket/access_key/secret_key` and use any S3 SDK.
`bootstrap.md` states "S3-compatible blob storage" as part of the non-negotiable contract, so this needs Chas to amend it.

Options:

| Option | What tenants get | Cost / effort | Verdict |
|---|---|---|---|
| **1. Azure-native refs** (`storage.account`, `storage.container`, `storage.endpoint`, `storage.sas_token`, optional `storage.container_sas_url`) | Apps must use an Azure Blob SDK or plain HTTPS+SAS. **Every S3-SDK app breaks**, including `examples/wordpress` (restore-on-boot does SigV4 with curl) and Ghost's S3 adapter. | Cheapest to run. Examples need Azure variants (Ghost has community Azure adapters; WordPress needs a rewrite of the restore path): M extra. | **Recommended if we port**, with the contract amended to "object storage". Honest and simple. |
| 2. s3proxy gateway in-cluster | S3 SDKs keep working | s3proxy (jclouds, JVM) is a long-running pod costing node memory (about 256-512 MiB EST). It has **one global identity/credential pair**, so per-tenant isolation needs one gateway per app (too heavy) or a custom auth shim (L). Presign works through it, but quota enforcement and SAS state still run through our code. | Not recommended. Adds a shared-fate component and an auth gap. |
| 3. Keep S3 elsewhere (Spaces, Cloudflare R2, Backblaze B2) | Unchanged S3 refs | Multi-cloud, cross-cloud egress charges from Azure at $0.087/GB. Spaces stays $5. | Only sensible as a hybrid fallback; defeats the point of moving. |
| 4. Commercial S3 layer on Marketplace (Flexify.IO) | S3 SDKs | Per-hour VM licence, not priced here | Skip. |

Design for option 1 (`AzureBlobStore`, `azure-storage-blob` SDK):

- **One shared storage account, one container per app** (`pp-<id>`). Storage accounts have no monthly fee. The default limit is 250 per
  region per subscription (EST), so a per-app storage account would also work and give a stronger blast radius, but costs 10-30 s per
  provision and naming/RBAC overhead; the shared account is simpler. Container count is not a practical limit (DO's 100-bucket wall disappears).
- **Credential = service SAS on the container with a stored access policy.** Reasons: the policy can be edited or deleted after
  issue, which revokes the SAS (a user-delegation SAS cannot be revoked short of rotating the delegation key); a stored access
  policy lets expiry be far in the future (Azure caps the policy count at 5 per container, EST). Map onto our interface:
  - `create_bucket_and_key` creates the container, a policy `rw-<gen>`, and a SAS referencing it. `access_key` column stores the policy id, `secret_key_encrypted` stores the SAS token.
  - `set_read_only(app, True)` **edits the policy permission in place** (`rwdl` to `rl`). No key swap, no new creds, returns `None`. This removes the persist-then-revoke dance in `quota.py` (issue #2 class of bugs) for Azure.
  - `rotate_key` creates policy `rw-<gen+1>` plus a new SAS and returns it; `revoke_other_keys` deletes the old policy (two live policies at most, under the 5 cap).
  - `delete` deletes the container (soft delete should be **disabled** on this account or deleted tenant data lingers and bills).
- The platform signs with the **account key**, held in the `picopaas-env` Secret like `SPACES_SECRET_ACCESS_KEY` today. Service SAS needs shared-key access, so do not enable "Entra-only / disable shared key" on this account. Control-plane calls (create container) could use Workload Identity with Storage Blob Data Contributor; setting stored access policies via Entra auth should be verified (EST).
- **Quota enforcement.** Azure has no per-container quota, same as Spaces. `bucket_size_bytes` and `usage` list blobs (5,000 per page) and sum `Content-Length`; `summarize_objects` in `spaces.py` is reusable. Account-level `BlobCapacity` metrics are daily and per account, too coarse. `delete_objects` uses the platform key, unchanged in spirit.
- **Presigned transfers (`/apps/{id}/storage/presign`).** Return a blob-level SAS (permissions `c`+`w` for upload, `r` for download, short expiry) as `url`, with `headers` `{"x-ms-blob-type": "BlockBlob"}` for PUT. **SAS cannot enforce Content-Length.** DO's signed Content-Length gave a hard per-upload cap. On Azure we lose that. Mitigations, in order:
  1. keep the existing pre-check against remaining quota when issuing the SAS;
  2. keep expiry short (minutes);
  3. add `POST /apps/{id}/storage/presign/complete` (and run the same check in the poll): read the blob's size with `get_blob_properties` and delete it if it pushes the container over the cap;
  4. for a malicious client, single-shot Put Blob accepts up to 5,000 MiB (EST), so a tenant can transiently exceed the cap by GBs until the next check; bound it with a faster poll for apps that recently minted an upload SAS. Cost of such abuse is cents, but it is a behaviour change to document.
- Tenant-visible behaviour change list: new ref names, new docs, CLI `storage upload|download` unchanged from the user's view (it calls our presign endpoint), UI ref picker, `env_refs.py`, API docs, spec schema, `examples/ghost`, `examples/wordpress`.

### 3.2 MySQL and PostgreSQL providers (M, 2d including a live spike)

MySQL Flexible Server (EST unless noted):

- The admin user is not SUPER (same class as `doadmin`). `mysql.py` already handles this: direct `GRANT ALL ON app_N.*`, `ALTER USER ... WITH MAX_USER_CONNECTIONS`, and the REVOKE/GRANT read-only toggle all work without SUPER. The session-kill path already tries the app user first and then admin, and `MYSQL_ADMIN_USE_PROCEDURES=1` exists with `picopaas_ops.*` definer procedures as an escape hatch if kill-as-admin is denied. Azure also ships `mysql.az_kill` / `mysql.az_kill_query` procedures, which would be a third path (add behind a config flag if the spike shows `KILL` fails).
- `sql_require_primary_key`: Azure does not force it ON by default (EST), but `sql_generate_invisible_primary_key=ON` is the Azure default (VERIFIED in the server parameters page), which makes keyless tables work. This is friendlier than DO for apps like Wiki.js. Document the difference.
- TLS: `require_secure_transport` ON by default. The CA is a **public** root chain (DigiCert Global Root G2 and Microsoft roots, EST), not a per-cluster CA, so `bootstrap`'s "fetch the DB CA from the DO API" step becomes "ship a fixed CA bundle". `mysql_ssl_context` keeps `VERIFY_X509_STRICT` cleared, which is harmless.
- Networking. Two choices: **private access** (VNet-integrated, delegated subnet plus a private DNS zone `*.mysql.database.azure.com`, no extra cost, cannot be changed to public later) or **public access with a firewall rule** for the cluster's outbound IP. Recommendation: private access, same VNet as AKS, so the DB has no internet exposure (parity with DO's private VPC). `MYSQL_EGRESS_CIDR` becomes the delegated subnet CIDR; the DB port is 3306 / 5432 and already comes from the `DbServer` row, so only `scheduler.py`'s default `25060` needs the cloud table. With Cilium, `ipBlock` CIDRs for **node and pod IPs** are not honoured by Cilium (VERIFIED, [AKS Cilium docs](https://learn.microsoft.com/azure/aks/azure-cni-powered-by-cilium)); a DB in a separate delegated subnet is neither, so the existing `ipBlock` allow rule should work. Needs a spike.
- Connection model improves: B1ms 171 default connections vs DO's ~76; tune `DB_POOL_SIZE` comment in `docs/deployment/infrastructure.md`.

PostgreSQL Flexible Server (EST unless noted):

- The admin role (`azure_pg_admin`) is not a superuser but holds CREATEROLE/CREATEDB, like `doadmin`. `postgres.py` should work with: `ADMIN_DB` changes from `defaultdb` to `postgres`; verify the ADMIN OPTION-on-created-roles behaviour (PG16+ `createrole_self_grant`), the `REVOKE CONNECT ... FROM PUBLIC` hardening, and that `default_transaction_read_only` is not used (the module already avoids it).
- TLS `sslmode=verify-ca` with the public root bundle. Burstable has no PgBouncer (VERIFIED), so tenants share 35 user connections on B1ms.

### 3.3 Kubernetes layer (M, 2-3d)

- **Network policy engine.** Use **Azure CNI Powered by Cilium** (`network_dataplane=cilium`, `network_policy=cilium`). Azure Network Policy Manager is retired on Linux from 2028-09-30 (VERIFIED) so it is not an option. Our isolation model (default deny, allow Traefik, egress `0.0.0.0/0` except RFC1918/link-local/reserved, per-app DB CIDR) depends on `ipBlock` with `except`. Under Cilium (VERIFIED from the AKS docs) `ipBlock` does not match **pod or node IPs**, even inside the CIDR, so the "internet except private" rule is, if anything, tighter. Also behaviour change: under Cilium a deny-all blocks NodePort/LoadBalancer traffic unless explicitly allowed (VERIFIED), so the existing "allow from Traefik" ingress rules need to also admit the LB health probe path; test early. `endPort` ranges need Cilium 1.17+ (VERIFIED); our `_internet_egress_ports` uses `endPort`, so confirm the AKS Cilium version.
- **`kubernetes.io/egress-bandwidth`** (20 Mbit default per pod). Cilium's bandwidth manager honours it upstream, but I could **not** confirm AKS enables the bandwidth manager (upstream default is off). UNVERIFIED. If it is not available, tenant egress is uncapped, which interacts badly with R3. Spike on a scratch cluster before committing.
- **Storage.** `VOLUME_STORAGE_CLASS=managed-csi`, `VOLUMES_MAX_TOTAL` must drop from 10 to about 3-4 per 2-vCPU node (4 data disks per VM, EST; the OS disk is separate), and `docs/deployment/infrastructure.md` plus `0007` need the per-cloud numbers. Disks are zonal: keep a single zone or pin the pool to one zone, otherwise a rescheduled PVC pod can get stuck (EST). Storage rounding changes the billing story ($0.30 per 1 GiB request).
- **Ingress / LB.** `deploy/traefik/values.yaml`: replace `service.beta.kubernetes.io/do-loadbalancer-*` with Azure annotations (`service.beta.kubernetes.io/azure-load-balancer-health-probe-request-path`, `azure-pip-name` for a pre-created static IP, resource group annotation if the IP lives outside the node RG). DO runs the LB in TCP mode without PROXY protocol; on Azure set `externalTrafficPolicy: Local` to keep the client IP for the login rate limiter (`config.py:223` documents that DO shows the LB IP today). That is a small improvement.
- **Pods and PSA.** Nothing DO-specific in the securityContext/PSA settings; AKS supports Pod Security Admission. metrics-server is installed by AKS already; `deploy/metrics-server` can be dropped (or left as a no-op) and `cluster_snapshot` keeps working.
- **Node memory.** AKS reserves memory on a regressive formula (EST ~1 GB on a 4 GiB node, plus eviction threshold), so allocatable is lower than the DOKS 4 GiB node's. Capacity numbers in `infrastructure.md` need re-measurement.
- **Autoscaler.** `max_nodes` ceiling carries over (`auto_scaling_enabled`, `min_count`/`max_count` on the node pool).

### 3.4 Terraform, workflows, DNS, secrets, CI auth (L, 6-8d)

- **Terraform (`azurerm`, M-L):** resource group; VNet with an AKS subnet and a DB delegated subnet; AKS (Free tier, Cilium dataplane, OS disk 32 GiB, `outbound_type = loadBalancer`, auto-upgrade channel, maintenance window equivalent of "Sunday 04:00"); user-assigned identity for Workload Identity; MySQL and PG flexible servers with private DNS zones; storage account + platform backup container; static public IP for Traefik. State stays in TFC (`azurerm` provider swaps in `infra/versions.tf`). Keep `prevent_destroy` on the cluster and DBs. DO files move to `infra/do/` while the migration runs.
- **Workflows (M):** `terraform.yml`, `bootstrap.yml`, `deploy.yml`, `diag.yml` move from `doctl` + `DIGITALOCEAN_TOKEN` to `azure/login` with OIDC (`id-token: write`, federated credential bound to repo and `main`), then `az aks get-credentials` (AAD/kubelogin if local accounts are disabled; leaving local accounts on is simpler and matches today's kubeconfig model). Replace the `doctl compute load-balancer` / `domain records` / `databases connection` calls with `az network public-ip show`, DO DNS API (kept) and `az mysql flexible-server show`. `bootstrap.yml` already isolates these in labelled steps, so it is a rewrite of steps, not of the flow.
- **DNS and certificates (S):** keep DO DNS and the in-tree DO DNS-01 solver (zero change to wildcard issuance). The alternative, cert-manager's Azure DNS solver with Workload Identity, is documented and works for wildcards but needs the NS cutover at the registrar. Custom domains stay HTTP-01.
- **Secrets (S):** same k8s Secrets as today (`picopaas-env`, `picopaas-db-ca`). Key Vault is optional and adds ~$0.03/10k operations plus the CSI driver; skip until needed. Sensitive Terraform outputs stay out of the repo as now.
- **GitHub secrets removed:** `DIGITALOCEAN_TOKEN`, `SPACES_*` (replaced by repo variables `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, which are identifiers, not secrets; only the storage account key and DB admin passwords remain secrets).

### Effort summary

| Block | Effort | Agent-days |
|---|---|---|
| 3.0 Seam refactor and `CLOUD` split | M | 2 |
| 3.1 `AzureBlobStore`, SAS design, presign, quota, tests (Azurite for local) | L | 5 |
| 3.1 Refs, UI, CLI, docs, ghost and wordpress examples for Azure-native storage | M-L | 3 |
| 3.2 DB provider spikes, TLS, networking, config | M | 2 |
| 3.3 Kubernetes (Cilium policy test, bandwidth, storage, Traefik/LB) | M | 3 |
| 3.4 Terraform | L | 4 |
| 3.4 Workflows, OIDC, DNS/cert wiring | M | 3 |
| Live-tenant migration tooling and rehearsal (section 5, phase 2) | M | 3 |
| Docs (infrastructure, runbooks, admin guide, ARCHITECTURE cost section) | S | 1 |
| **Total** | | **~26** |

---

## 4. Risks and unknowns

| # | Risk | Severity | Mitigation |
|---|---|---|---|
| R1 | **Storage contract.** Azure Blob is not S3; tenant apps using S3 SDKs break; `bootstrap.md` requires S3-compatible storage. | High, product-level | Chas decides: Azure-native refs (recommended) or hybrid. See 3.1. |
| R2 | **Cost goes up** on AKS (+$29 to +$46/mo) because B-series cannot be a system pool and the Standard LB is mandatory. | High | Variants C/D, or a sponsored subscription. |
| R3 | **Egress is metered** ($0.087/GB after 100 GB/mo, subscription-wide) while DO bundles an allowance. At the 20 Mbit per-pod default a single hostile app could in theory push ~6.5 TB/mo (about $560). | High | Confirm the bandwidth annotation works on AKS; add a Cost Management budget with alerts (free); add a per-app monthly transfer cap or lower `EGRESS_BANDWIDTH_MBIT`. Blob egress via SAS is billed the same way. |
| R4 | System pool minimum size and node count (docs conflict: 2 vCPU/4 GB on one page, 4 vCPU and 2 nodes on another). | High if 4 vCPU x 2 nodes | Create a scratch cluster and try 1 x D2als_v6 before any commitment. |
| R5 | Bandwidth annotation and `endPort` support depend on the Cilium version AKS ships. | Medium | Spike. |
| R6 | 4 data disks per 2-vCPU VM vs 15 on DOKS: persistent volumes (request 0007) shrink from 10 to about 3 per node. WordPress-style apps compete for slots. | Medium | Per-cloud `VOLUMES_MAX_TOTAL`; document. |
| R7 | Azure Disk is zonal and slower to reattach; Standard SSD tier rounding (1 GiB request billed as 4). | Low-Medium | Single zone; accept. |
| R8 | B1ms databases are burstable (credit-based CPU, 2 GiB RAM). Many tenants on one B1ms could throttle the platform DB itself. | Medium | Keep platform DB and tenant DBs on separate servers if tenant count grows (see `docs/research/db-scaling.md`). |
| R9 | Upload size cap can no longer be hard-enforced at presign time (SAS). | Medium | Post-upload check (3.1); document. |
| R10 | Stored access policy limit (5 per container) and shared-key dependency for service SAS (Entra-only mode unusable). | Low | Design uses 2 live policies max. |
| R11 | Portal defaults enable paid add-ons (Container Insights/Log Analytics, Defender). | Low | Terraform only; leave monitoring addons off. |
| R12 | AKS Free tier has no SLA and a "fewer than 10 nodes" recommendation; Azure also reboots/upgrades nodes (auto-upgrade) with the same brief blip as DOKS. | Low | Matches current stance; maintenance window. |
| R13 | A scratch Azure subscription costs money while it exists (an idle AKS cluster can be stopped, but disks, LB, IPs and DBs keep billing). | Low | Time-box; a new request file would be needed before any paid resource (see below). |

**EST items to verify in a spike** (not confirmed in this session): AKS 1-node system pool, `managed-csi` class properties, per-VM data-disk
limit of 4 from Learn, Azure DNS prices, PG 32 GiB minimum storage, LB billing when only outbound rules exist, one vs two public IPs,
AKS memory reservation, public-CA chain for MySQL/PG TLS, `az_kill` procedures, `sql_require_primary_key` default, storage-account count limit, stored-access-policy
management under Entra auth, Cilium bandwidth manager default on AKS, free-account offers beyond MySQL.

---

## 5. Phased plan

**Phase 0: seam refactor, DO stays live (2d, $0).** `CLOUD` config split, `object_store_for()` factory, generalised storage creds
shape, per-cloud defaults table, contract tests that both stores must pass against the fake. No behaviour change on DO. Land
this regardless of the Azure decision.

**Phase 1: Azure behind config in a scratch subscription (~15 agent-days, plus paid scratch infra).**
Needs a request file (`docs/requests/NNNN-azure-scratch.md`, format: what / why / cost) and Chas's APPROVED before anything is created. Proposed shape: AKS Free, 1 node,
B1ms MySQL, storage account, no PG at first, torn down after each session (an idle stack costs roughly $3.50/day, so budget ~$15-25 for a
focused two-week spike, mostly the node, LB, IPs and DB). Exit criteria: all of `tests/` pass against Azurite, a canary app deploys with its own DB and
bucket, Cilium policies verified (including the DB CIDR and `endPort`), bandwidth annotation verified, PVC attach/reclaim verified, `terraform validate`/`plan` clean.

**Phase 2: migrate live tenants (3d + a rehearsal).**

1. Freeze signups; announce a short maintenance window (platform is a throwaway PaaS, tenants expect it).
2. DBs: per app, `mysqldump` / `pg_dump` from DO into the Azure server (the platform already holds admin creds for both; size cap is 50 MB so each dump is seconds). Recreate roles with new passwords through the provider and update stored creds.
3. Blob: if option 1, there is no S3 compat, so copy objects with `azcopy` (supports S3 as a source) into each tenant container; tenants must update code to the Azure SDK, so announce **before** cutover and run both refs for a grace period if feasible. Cap is 100 MB per app so volume is small.
4. Volumes: not backed up today; copy via a temporary pod (`tar` over `kubectl exec`) per app, or let tenants re-create.
5. Platform DB: dump/restore the `picopaas` database, then flip `DATABASE_URL`.
6. DNS cutover: change the two A records (`*`, `picopaas`) at DO to the Azure static IP (TTL is already low). cert-manager reissues the wildcard (DNS-01 unchanged). Custom-domain tenants must repoint their CNAME if it targets `lb.<domain>`; keep `lb.` as the stable target so only one record moves.
7. Run `examples.yml`/`quota-e2e.yml` against Azure, then decommission DO after a hold period.

Fallback: keep DO running (idle cost $75/mo) for one week; DNS flip back is one record.

---

## 6. Verdict

- **Effort:** about **26 agent-days** for a complete AKS port, of which ~8 are object storage and ~7 are Terraform and workflows; ~2 are the refactor worth doing anyway.
- **Cost:** **up, not flat.** AKS like for like about **$121/mo vs $75.30** (+60%); cheapest viable AKS about **$104** (+39%) without PostgreSQL. The cause is structural (no B-series system pool, mandatory $18 LB, $0.30 minimum disk billing, metered egress), not a pricing accident. Azure is cheaper only in managed DBs on paper and in object storage ($0.50 vs $5).
- **Doing better:** an Azure VM running k3s with managed MySQL/PostgreSQL and Blob is about **$68/mo** ($52 without PostgreSQL, $38 with in-cluster MySQL), under today's baseline, at the cost of self-managing the cluster.
- **Recommendation:** **do not port for cost.** If there is a non-cost reason (sponsored subscription, learning goal, tenancy), do phase 0 now (cheap, valuable), then a time-boxed phase 1 spike with a request file, and prefer Azure-native storage refs with an amended contract. Choose AKS only if the credits cover ~$120/mo; otherwise the VM + k3s shape is the one that fits the budget.
- **Questions for Chas** (also put in `docs/QUESTIONS.md` if he wants to pursue this): is the Azure subscription sponsored, and what is the monthly credit; is he willing to amend "S3-compatible" to "object storage" in `bootstrap.md`; is a self-managed k3s VM acceptable.

### Sources

- Azure Retail Prices API, queried 2026-10-23, `armRegionName eq 'eastus2'` (VMs, MySQL/PG Flexible Server, Standard Load Balancer, public IPs, Blob, managed disks, AKS uptime SLA): https://prices.azure.com/api/retail/prices
- AKS tiers: https://learn.microsoft.com/en-us/azure/aks/free-standard-pricing-tiers
- AKS VM restrictions: https://learn.microsoft.com/en-us/azure/aks/quotas-skus-regions and https://learn.microsoft.com/en-us/azure/aks/use-system-pools
- AKS Cilium and NPM retirement: https://learn.microsoft.com/azure/aks/azure-cni-powered-by-cilium
- MySQL Flexible Server parameters and limits: https://learn.microsoft.com/en-us/azure/mysql/flexible-server/concepts-server-parameters
- MySQL pricing and free offer: https://azure.microsoft.com/pricing/details/mysql and https://learn.microsoft.com/azure/mysql/flexible-server/overview
- PostgreSQL Flexible Server limits: https://learn.microsoft.com/en-us/azure/postgresql/flexible-server/concepts-limits
- Bandwidth pricing: https://azure.microsoft.com/en-us/pricing/details/bandwidth/
- Cilium bandwidth manager: https://docs.cilium.io/en/stable/network/kubernetes/bandwidth-manager/
- S3 on Azure (third-party): https://www.tigrisdata.com/docs/migration/azure-blob/, https://devblogs.microsoft.com/ise/2016/05/22/access-azure-blob-storage-from-your-apps-using-s3-api/, https://marketplace.microsoft.com/en-us/product/virtual-machines/flexify.azure-s3-api
