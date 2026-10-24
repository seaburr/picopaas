# Porting PicoPaaS to AWS (EKS + RDS + S3)

Research only (2026-10-23, [infra-ops]). Nothing was provisioned or applied; no AWS or DigitalOcean account was touched, and the local kubectl
context was not used. Question from Chas: what would it take to run PicoPaaS on EKS, RDS and S3, and can it stay inside the current
~$60-75/mo window or do better? This is the AWS twin of [azure-port.md](azure-port.md); sections and cost lines are numbered the same way so the two
compare directly.

**Short answer.** The code port is about **20 agent-days** (Azure: ~26), and it is much simpler than Azure for one reason: **S3 is
natively S3.** Tenant S3 SDKs, the WordPress SigV4 restore, Ghost's S3 adapter and the `bootstrap.md` "S3-compatible storage"
contract all keep working with a config change (endpoint, region, credentials). The bill is the problem: on EKS it goes **up**, from
$75.30 to about **$155** (like for like) or **$120** (cheapest viable, no PostgreSQL), because the EKS control plane alone is $73/mo and
an NLB plus IPv4 addresses are not free. **Skipping EKS** (one EC2 instance running k3s, RDS, S3) lands at about **$62** ($48 without
PostgreSQL, $35 with in-cluster MySQL), under DO, at the price of running the control plane ourselves.

## How to read the prices

- **VERIFIED** = pulled from the AWS Price List bulk API (`https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/<Service>/current/us-east-1/index.json`
  for AmazonEC2, AmazonEKS, AmazonRDS, AmazonVPC, AWSELB, AmazonS3, AmazonLightsail; region `us-east-1`, USD, on-demand list price,
  queried 2026-10-23) or an AWS documentation page fetched the same day. Monthly = hourly x 730 h.
- **EST** = recalled from AWS documentation or inferred without a fetch in this session. Needs a spike before anyone relies on it. Every EST
  is listed again in section 4.
- DO figures are the ones in `docs/ARCHITECTURE.md` section 1 and `docs/deployment/infrastructure.md` ($75.30/mo baseline). Region: us-east-1
  (closest to NYC3). Free tier and credits are mentioned but **never counted**.
- Architecture caveat that drives every node line: tenants ship **one Docker image**, almost always `linux/amd64`. So the node is **x86**
  (t3a/t3), not Graviton (t4g). A t4g.medium is $24.53 instead of $27.45 but silently breaks every amd64-only tenant image. Graviton is fine for RDS
  (managed, no tenant images).

---

## 1. Service mapping (DO to AWS)

| Concern | Today (DO) | AWS equivalent | Price (us-east-1) | Notes |
|---|---|---|---|---|
| Kubernetes control plane | DOKS non-HA, $0 | **EKS standard**: $0.10/h = **$73.00/mo** (VERIFIED, `AmazonEKS-Hours:perCluster`) | $73.00 | No free tier for the control plane; this is the dominant cost. After a Kubernetes version leaves standard support (about 14 months, EST), **extended support is +$0.50/h = +$365/mo** (VERIFIED, `AmazonEKS-Hours:extendedSupport`), so the cluster must be upgraded about once a year. DOKS auto-upgrades; EKS does not. |
| EKS Auto Mode | n/a | Management fee on top of the EC2 price: t3a.medium $0.00451/h = **+$3.29/mo** per node, t4g.medium $0.00403/h (VERIFIED) | +$3.29/node | Auto Mode bundles Karpenter, VPC CNI with network policy, EBS CSI, load-balancer integration, Bottlerocket nodes with a **21-day max lifetime**, no SSH/SSM (VERIFIED, [Auto Mode docs](https://docs.aws.amazon.com/eks/latest/userguide/automode.html)). You cannot swap the CNI (so no Cilium) and burstable T-family availability in the default NodePools is not confirmed (EST). Not recommended here: less control, same $73. Managed node groups are the pick. |
| Worker node | 1 x `s-2vcpu-4gb-amd`, $28 | **t3a.medium** (2 vCPU / 4 GiB, AMD) $0.0376/h = **$27.45**; t3.medium (Intel) $0.0416/h = $30.37; t3a.large (8 GiB) $0.0752/h = $54.90; t3.large $0.0832/h = $60.74 | $27.45 | VERIFIED prices. Almost identical to DO's $28 for the same shape. Burstable, like DO's shared CPU (baseline 20% per vCPU, EST). **Run in standard credit mode** (default for t3/t3a launch templates is `unlimited`, which bills surplus credits). Arm: t4g.medium $0.0336/h = $24.53, t4g.large $0.0672/h = $49.06 (VERIFIED) but arm64 only, see above. |
| Spot node | n/a | Spot t3a.medium is roughly 60-70% off (EST) | about $9-11 | A single evictable node means platform downtime on eviction. Not recommended for the only node; possible for an autoscaled second node. |
| Node OS disk | included | gp3 root volume, $0.08/GB-mo (VERIFIED); EKS AMI default 20 GiB (EST) | $1.60 | |
| Load balancer | DO LB `lb-small`, $12 flat | **NLB** $0.0225/h = **$16.43/mo** plus LCUs at $0.006/LCU-h (VERIFIED); ALB is the same hourly rate with $0.008/LCU-h (VERIFIED). Classic LB $0.025/h. | $16.43 + about $1 LCU (EST at our traffic) | Plus the public IPv4 on the NLB (next row). Traefik terminates TLS and the wildcard cert, so an L4 NLB is correct and ALB is pointless. NLB hands out a **DNS name**, not an IP; bootstrap's A-record upsert needs EIP-backed NLB subnets (one EIP per AZ) or CNAMEs. |
| Load balancer, skip | n/a | `hostPort`/`hostNetwork` Traefik on the single node with one Elastic IP | $0 extra | Works because there is one node. Node replacement loses the EIP association unless a small script/launch hook re-attaches it (not free to build). A hack, usable for "cheapest viable". |
| Public IPv4 | included with LB | **$0.005/h = $3.65/mo per address**, in-use or idle (VERIFIED, `USE1-PublicIPv4:InUseAddress`) | node $3.65 + NLB $3.65 per AZ used | Applies to the node's public address (needed because there is no NAT) and each NLB AZ. NLB in one AZ keeps it to one address; two AZs doubles it (EST on how the console defaults). |
| NAT gateway | n/a | $0.045/h = **$32.85/mo** plus $0.045/GB (VERIFIED) | avoid | Put nodes in **public subnets** with a public IP and a security group that allows only 80/443 in. GHCR pulls, Let's Encrypt, S3 and the DO API are all outbound-internet anyway. A free **S3 gateway endpoint** (EST: no charge) keeps S3 traffic off the public path. |
| Managed MySQL | `db-s-1vcpu-1gb` $15.15 (10 GiB) | **RDS MySQL db.t4g.micro** (2 vCPU burst, 1 GiB) $0.016/h = $11.68/mo; db.t4g.small (2 GiB) $0.032/h = $23.36; db.t4g.medium (4 GiB) $0.065/h = $47.45 (all Single-AZ, VERIFIED). gp3 storage $0.115/GB-mo (VERIFIED), 20 GiB minimum for gp3 (EST) = $2.30 | **$13.98** (micro) | Backups free up to the provisioned storage size (EST, per RDS pricing). Default `max_connections` = `DBInstanceClassMemory/12582880`; AWS documents **about 60** on a t3.micro and "about 630" on an 8 GiB class (VERIFIED, [RDS limits](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/CHAP_Limits.html)). t4g.micro is assumed to be about 60 (EST). DO gives about 76. A t4g.small (~150, EST) is the sane size once tenants multiply. |
| Managed PostgreSQL | `db-s-1vcpu-1gb` $15.15 | **RDS PostgreSQL db.t4g.micro** $0.016/h = $11.68; t4g.small $0.032/h = $23.36 (VERIFIED); storage $0.115/GB-mo, 20 GiB = $2.30 | **$13.98** | Default `max_connections` = `LEAST(DBInstanceClassMemory/9531392, 5000)` (VERIFIED formula, so roughly 60-80 on a 1 GiB class, EST). |
| Aurora Serverless v2 | n/a | **$0.12/ACU-h** (MySQL and PostgreSQL, VERIFIED), minimum 0.5 ACU = $43.80/mo while running; storage $0.10/GB-mo and I/O extra (EST) | $44+ per cluster | Can pause to 0 ACU (EST), but the platform database is always active so it never pauses. Costs about 3x a db.t4g.micro. Not viable. |
| Object storage | Spaces $5 flat (250 GiB, 1 TiB egress) | **S3 Standard**: $0.023/GB-mo first 50 TB; PUT/COPY/POST/LIST $0.005 per 1,000; GET $0.0004 per 1,000 (VERIFIED) | ~$0.50/mo at our scale | **Native S3.** No monthly minimum, usage-based. Spaces drops from $5 to about $0.50. Default bucket quota is **10,000 per account**, raisable to 1M (VERIFIED, [S3 announcement](https://aws.amazon.com/about-aws/whats-new/2024/11/amazon-s3-up-1-million-buckets-per-aws-account/)), so DO's 100-bucket wall disappears. |
| DNS | DO DNS $0 | Route 53 public zone **$0.50/zone/mo** + $0.40 per million queries (EST for the zone fee: the zone line did not appear in the price-list slice we parsed; $0.40/M queries is VERIFIED for resolver queries only) | ~$0.50 | **Option 1: leave DNS on DO** ($0, the DO DNS-01 solver and the `bootstrap` A-record step keep working, but a DO API token stays in the cluster). **Option 2: Route 53** with a cert-manager Route 53 solver via Pod Identity (no static token anywhere, but needs a registrar NS change and a hosted-zone import). |
| Block volumes (PVC) | `do-block-storage`, $0.10/GiB, min 1 GiB, 15 per node | **EBS CSI add-on, gp3**: $0.08/GB-mo (VERIFIED), 1 GiB minimum (EST), billed per provisioned GiB | 1 GiB request = $0.08 | Cheaper than DO and no tier rounding (Azure rounds a 1 GiB request up to a 4 GiB tier). EKS does not ship a gp3 StorageClass; we create one. Nitro instances share **28 attachments across EBS volumes, ENIs and NVMe store** (VERIFIED for "most Nitro types", [EBS volume limits](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/volume_limits.html); t3a/t4g are not in the dedicated-limit list, so the shared rule applies, EST). A medium node uses 2-3 ENIs, so about 25 volumes, well above our `VOLUMES_MAX_TOTAL` of 10. Volumes are AZ-bound (see R7). |
| Pods per node | 110 (DOKS default) | VPC CNI default: **t3a.medium / t3.medium 17 pods**, t3.large 35, t4g.small 11 (VERIFIED, [eni-max-pods.txt](https://github.com/aws/amazon-vpc-cni-k8s/blob/master/misc/eni-max-pods.txt); t3a has the same ENI limits as t3, EST) | n/a | Platform pods (aws-node, kube-proxy, ebs-csi-node, coredns x2, traefik, cert-manager x3, platform x2, metrics-server) eat most of 17. **Needs prefix delegation** (up to 110) or tenants cannot be scheduled; see R2. |
| Container registry | GHCR | GHCR unchanged | $0 | Anonymous pulls from GHCR work from any cluster; no ECR needed. |
| Internet egress | droplet transfer allowance pooled, overage $0.01/GiB | **$0.09/GB** to the internet (VERIFIED as the rate AWS cites on the VPC pricing page); AWS gives a free monthly allowance (100 GB aggregated across services, EST: not confirmed in this session); inbound free; same-region S3 and RDS traffic free (EST) | | The largest new unbounded cost, the same shape as Azure R3. See R3. |
| State | Terraform Cloud | TFC keeps working with the `aws` provider, or an S3 backend with native lockfile (~$0) | $0 | |
| CI credentials | long-lived `DIGITALOCEAN_TOKEN` + `SPACES_*` GitHub secrets | **GitHub Actions OIDC to an IAM role** (`aws-actions/configure-aws-credentials`, no secret stored) | $0 | Security improvement; the role ARN is an identifier, not a secret. |
| Platform pod credentials | account-wide DO token + full-access Spaces key in `picopaas-env` | **EKS Pod Identity** (or IRSA): a scoped role with temporary credentials | $0 (EST: no add-on charge) | Closes the "account-wide token" item in `docs/QUESTIONS.md`. See 3.4. |
| Monitoring | none | Leave EKS control-plane logging, Container Insights and GuardDuty off; CloudWatch bills per GB (EST) | $0 | The console and `eksctl` can pre-tick some of these. |
| Lightsail (non-EKS) | n/a | Instance bundles: 4 GB (2 vCPU) $0.03225/h = **$23.54/mo**, 8 GB $0.05913/h = $43.16; managed DB 1 GB $0.02016/h = **$14.72**, 2 GB $0.04032/h = $29.43; LB $0.0242/h = $17.66 (all VERIFIED) | | Instance bundle includes a public IPv4 and a transfer allowance (size EST: 4 TB on the 4 GB bundle). **No instance profile**: a Lightsail VM cannot assume an IAM role, so S3/IAM calls need long-lived IAM user keys, which defeats the Pod Identity security win. |

---

## 2. Cost model

All figures USD per month, us-east-1, 730 h, 2026-10-23. "Like for like" = the same shape as today (one app node, managed MySQL, managed
PostgreSQL, public LB, object storage). Compute row = one node that runs both system components and tenant apps. DNS stays on DO ($0) in
every column; moving to Route 53 adds about $0.50.

| Line | DO today | AWS A: like for like (EKS standard) | AWS B: cheapest viable EKS | AWS C: EC2 + k3s, RDS | AWS D: EC2 + k3s, in-cluster MySQL, no PG | AWS E: Lightsail + k3s |
|---|---|---|---|---|---|---|
| Control plane | $0 | $73.00 | $73.00 | $0 (we run k3s) | $0 | $0 |
| Node | $28.00 | $27.45 (t3a.medium) | $27.45 | $27.45 | $27.45 | $23.54 (4 GB bundle) |
| Node OS disk | incl. | $1.60 (20 GiB gp3) | $1.60 | $2.40 (30 GiB gp3) | $2.40 | incl. (80 GB SSD, EST) |
| Load balancer | $12.00 | $16.43 + ~$1.00 LCU | $0 (hostPort + EIP) | $0 (hostPort + EIP) | $0 | $0 (hostPort) |
| Public IPv4 | incl. | $7.30 (node + NLB, one AZ) | $3.65 (one EIP) | $3.65 (one EIP) | $3.65 | incl. |
| MySQL | $15.15 | $13.98 (RDS micro) | $13.98 | $13.98 | $0.64 (8 GiB gp3 PVC) | $14.72 (Lightsail DB 1 GB) |
| PostgreSQL | $15.15 | $13.98 (RDS micro) | $0 (PG apps disabled) | $13.98 (or $0 if dropped) | $0 | $14.72 (or $0) |
| Object storage | $5.00 | ~$0.50 | ~$0.50 | ~$0.50 | ~$0.50 | ~$0.50 |
| DNS | $0 | $0 | $0 | $0 | $0 | $0 |
| **Total** | **$75.30** | **~$155.24** | **~$120.18** | **~$61.96** ($47.98 without PG) | **~$34.64** | **~$53.48** ($38.76 without PG) |
| vs DO | | **+$79.94 (+106%)** | **+$44.88 (+60%)** | **-$13.34 (-18%)** ($47.98: -36%) | **-$40.66** | **-$21.82 (-29%)** ($38.76: -49%) |

Notes on the columns.

- **A** carries three costs that DO bundles: the $73 control plane, the $16.43 NLB and the second IPv4 address. A t3a.large (8 GiB) in place
  of the medium is about $182.69. EKS Auto Mode would add $3.29/node and remove options (section 1), so it is not priced as its own column.
- **B** gives up PostgreSQL and the LB. Running the single-node Traefik on `hostPort` with an Elastic IP is a hack that must survive node
  replacement. With in-cluster MySQL instead of RDS it is about **$106.84** (8 GiB gp3 = $0.64, no RDS). The $73 control plane makes anything
  on EKS at least **$102.45** (control plane + node + root volume + one IP) before any database or storage.
- **C** is "EC2 + k3s": we give up the managed control plane, managed node upgrades and the EBS CSI/VPC CNI add-ons; we take on k3s upgrades,
  OS patching and single-node loss recovery (same exposure as DO's single node today). EC2 **does** have instance profiles, so the platform
  pod can use a scoped role (via Pod Identity-equivalent: IMDS with hop limit raised only for the platform pod, or a small credential refresher; EST, see R9).
  k3s embeds kube-router network policy (supports `ipBlock` with `except`, EST); the egress-bandwidth annotation needs the bandwidth CNI
  plugin or Cilium (EST, spike). The EBS CSI driver runs on k3s too (needs the node role), or use `local-path` (node-bound).
- **D** reuses the in-cluster MySQL manifests already in `deploy/mysql/`. It matches the original $40 target but gives up managed backups and
  failover; throwaway-only.
- **E** is the cheapest managed shape ($53.48 / $38.76) but the VM has **no IAM role**, so S3 and IAM provisioning would use static IAM user keys in a
  Secret (the same class of credential as today's DO token, narrower scope). Lightsail DBs are RDS under the hood (EST), so the provider code is identical.
- Free offers: AWS accounts opened after mid-July 2025 get $100 in credits (up to $200 with activities), valid 12 months, and the Free plan ends at six
  months or when the credits are gone; EC2 and RDS draw down the credits (the old 750 h/month micro offer applies only to older accounts)
  (secondary sources, EST: https://builder.aws.com/content/3BlPcDGddaB6WkPvUf1LIy1rzIm/everything-you-need-to-know-to-get-started-with-aws-free-tier).
  **Not counted.** If Chas has an employer or startup credit grant, A may cost him nothing. That is a question for him, not an assumption.

### Per-tenant marginal cost

| Resource | DO | AWS (A/B/C) | Comment |
|---|---|---|---|
| App compute | ~$2.50-3.50 (node $28 per 8-12 apps) | ~$2.30-3.40 on a $27.45 node, but **17 pods per node** until prefix delegation is on (about 5 tenant pods after system pods) | Pod ceiling, not CPU, is the real limit on EKS; fixable (R2). |
| MySQL DB (50 MB) | ~$0 (25 DBs per server) | ~$0 until the 20 GiB minimum is exceeded (100 x 50 MB = 5 GB). Connections: about 60 on micro; platform pool takes up to 33, leaving ~27 for tenants at 10 each = **2-3 apps**; t4g.small (~150) gives ~11 | Same ceiling as DO today (~76). Move to t4g.small ($25.66 with storage) sooner than DO. |
| PostgreSQL DB (50 MB) | ~$0, limited to ~4 apps by connections | ~$0; micro limited to about 12-16 apps at 5 per role (about 60-80 connections, EST) | Better than DO's ~22 usable. |
| S3 (100 MB) | $0 (inside $5) | 100 MB x $0.023 = $0.002. The 5-minute quota poll lists the bucket: 8,640 LIST/app/mo x $0.000005 = **$0.043/app/mo** (VERIFIED rate) | Identical to Azure's number. CloudWatch's daily `BucketSizeBytes` metric is free but one day stale, so it cannot replace the poll. |
| Volume (1 GiB request) | $0.10 | **$0.08**; 10 GiB admin override $0.80 | Best of the three clouds. Up to ~25 attached per node. |
| IAM identity | $0 (Spaces key) | $0 (IAM users and keys are free) | Hard limit: 5,000 IAM users per account (EST), 2 access keys per user. |
| Egress | pooled allowance | $0.09/GB past the free allowance | See R3. |

---

## 3. Code and platform changes, in order

Effort: S = under a day, M = 1-3 days, L = 3+ days (agent-days). The seam inventory is in [azure-port.md section 3.0](azure-port.md#30-seam-refactor-with-do-still-running-m-2d-do-this-first-it-pays-off-even-if-azure-never-happens); I re-checked
the object-store seam against `app/providers/spaces.py` (328 lines) and the findings below hold for AWS as well.

### 3.0 Seam refactor (M, 2d, shared with Azure; do once)

Same work as Azure phase 0, with one simplification: **AWS needs no new storage-creds shape**. The `{"kind": "s3"|"azure", ...}` proposal
stays, and AWS is `kind: "s3"` with the existing `endpoint/region/bucket/access_key/secret_key` refs, so `reconciler._storage_refs`, `env_refs.py`,
`AppStorage` columns, the UI picker, CLI `storage upload|download` and `examples/wordpress|ghost` are unchanged.

- `CLOUD=do|azure|aws` in `app/config.py` selects defaults and factories only. `object_store_for("aws")` returns `S3Store`.
- Split `SpacesStore` into a **shared S3 data plane** (`_iter_objects`, `summarize_objects`, `bucket_size_bytes`, `usage`, `delete_objects`,
  `presign`; all plain boto3/S3 calls) and a **per-cloud key plane** (DO key API vs IAM). `SpacesStore` keeps `DO_API` key calls; `S3Store`
  adds IAM. This is S on top of 3.0 and makes both stores share one tested data path.
- The per-cloud table gains an AWS column: storage class `gp3`, per-node volume cap, DB admin database (`mysql` for MySQL, `postgres` for PG,
  vs DO `defaultdb`), default DB ports 3306/5432 (vs DO 25060), CA bundle source, "provider configured" check (AWS: a role or
  `AWS_ROLE_ARN`/`AWS_CONTAINER_CREDENTIALS_FULL_URI` present, not `DIGITALOCEAN_TOKEN`).
- `FakeObjectStore` remains the contract test; `moto` (an in-process fake for S3 and IAM, a test-only dependency) lets the real `S3Store` run in `tests/`
  with no network, which is nicer than Azure's Azurite container.

### 3.1 Object storage: S3 via boto3, a small change (M, 3d; Azure needed 8d)

The data plane already speaks S3 against a custom endpoint (`https://<region>.digitaloceanspaces.com`). On AWS it is the standard
regional endpoint (`https://s3.<region>.amazonaws.com`), SigV4, same SDK. What changes is **credential provisioning**, which on DO
calls a DO key API that mints per-bucket scoped keys.

Design (`S3Store`, boto3 `s3` + `iam`):

- **One bucket per app, one IAM user per app.** Bucket `pp-<account-suffix>-app-<id>` (S3 names are global, so add a stable random suffix).
  IAM user `pp-app-<id>` under path `/picopaas/`, tagged `app=<id>`, with an inline policy scoped to `arn:aws:s3:::<bucket>` and `/*`
  (about 400 characters, well under the **2,048-character per-user inline cap**, VERIFIED, [IAM quotas](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_iam-quotas.html)).
  Per-bucket scoping is the isolation boundary; this is the same blast radius as DO's per-bucket keys.
- **Alternative rejected: one shared bucket with per-app prefixes.** It halves the resource count but tenants that list the bucket root,
  assume a bucket-level namespace or use bucket CORS/lifecycle (Ghost, WordPress restore, anything using `ListObjectsV2` without a prefix) break or
  become prefix-aware. It also makes `delete` and the quota poll prefix scans. Per-app buckets with a 10,000 default quota is simpler and keeps the contract.
- **Alternative rejected: STS short-lived credentials per app.** Tenant SDKs would need a refresh path; our refs are static strings.
- Map onto the existing interface:
  - `create_bucket_and_key`: `create_bucket` (Block Public Access on, default SSE-S3, versioning off), `create_user`, `put_user_policy` (read-write), `create_access_key`. `access_key` column = key id, `secret_key_encrypted` = secret.
  - `set_read_only(app, True)`: `put_user_policy` with a read-only policy **in place**. No key swap, so it returns `None`; the persist-then-revoke dance in `quota.py` (lines 98-146) is skipped, like Azure. IAM is eventually consistent (seconds, EST), acceptable for a quota gate polled every 5 minutes.
  - `rotate_key`: a user holds **at most 2 access keys** (EST), which matches "current + previous". `create_access_key` returns the new one; `revoke_other_keys` / `delete_key` call `delete_access_key`.
  - `delete`: empty then delete bucket (batch delete up to 1,000 per call, same loop shape as `delete_objects`), `delete_access_key`, `delete_user_policy`, `delete_user`. Versioning stays off so deletes do not leave noncurrent versions billing.
- **Quota enforcement.** S3 has no per-bucket quota, same as Spaces, so the poller stays. `bucket_size_bytes` lists with `ListObjectsV2` (1,000 per page, the existing `summarize_objects` reused); `delete_objects` unchanged in spirit.
- **Presigned transfers (`/apps/{id}/storage/presign`).** `generate_presigned_url('put_object', Params={..., 'ContentLength': n})` with SigV4 gives a **hard signed `Content-Length`**, the behaviour DO gave and Azure SAS cannot (Azure R9). No change to the endpoint contract. With role credentials (Pod Identity) the URL embeds `X-Amz-Security-Token` and expires with the session; keep expiries to minutes (max session 12 h, VERIFIED above). With IAM-user credentials the maximum is 7 days (EST).
- **Addressing style.** boto3 uses virtual-hosted style (`<bucket>.s3.<region>.amazonaws.com`) by default. The WordPress restore uses SigV4 with curl against a path-style URL; AWS still supports path-style (EST: its deprecation was announced then paused), so set `storage.endpoint` and use the region-specific host. Bucket region must equal the client region to avoid 301 redirects.
- **Tenant-visible changes: none beyond the endpoint/region values and the 63-character, globally unique bucket name.** The `storage.*` refs resolve to the new values on the next reconcile.
- **Where the bucket lives.** us-east-1 (same region as compute) makes node-to-S3 traffic free. Tenant egress over an S3 gateway endpoint stays in-region.

### 3.2 MySQL and PostgreSQL providers on RDS (M, 2d including a live spike)

RDS MySQL (EST unless noted):

- The master user has broad privileges but **not SUPER**, the same class as `doadmin`. `mysql.py` already handles this: `GRANT ALL ON app_N.*`, `ALTER USER ... WITH MAX_USER_CONNECTIONS`, the REVOKE/GRANT read-only toggle. For killing other users' sessions RDS provides `mysql.rds_kill` / `mysql.rds_kill_query`, the AWS equivalent of Azure's `mysql.az_kill`; add behind the existing `MYSQL_ADMIN_USE_PROCEDURES`-style flag if the spike shows `KILL` fails.
- Parameter group (free, but needed): set `require_secure_transport=1` (RDS MySQL defaults to off), consider `sql_generate_invisible_primary_key=ON` (keyless tables then work, like Azure; DO forces `sql_require_primary_key=ON` and breaks Wiki.js).
- TLS: RDS uses a **public, documented CA bundle** (`global-bundle.pem`, one file for all regions), not a per-cluster CA. Bootstrap's "fetch the DB CA from the DO API" step becomes "ship the fixed bundle" (same as Azure). `mysql_ssl_context` keeps `VERIFY_X509_STRICT` cleared.
- Networking: DB in **private subnets** (a subnet group needs two AZs, free), `PubliclyAccessible=false`, security group allowing 3306/5432 from the node security group. `MYSQL_EGRESS_CIDR` / `PG_EGRESS_CIDR` become the DB subnet CIDRs; tenant policy still needs the explicit allow because the DB sits in the same VPC as the pods. Pod IPs are VPC IPs, so keep DB subnets outside the pod subnets (see 3.3).
- Port and admin DB come from the per-cloud table (3306 / 5432, `mysql` / `postgres`).
- Connections: about 60 on micro (above). Raise `max_connections` in the parameter group only as far as memory allows; prefer t4g.small. RDS Proxy is not free (EST, priced per vCPU) and not worth it here.

RDS PostgreSQL:

- The master has `rds_superuser` (CREATEROLE, CREATEDB, can GRANT), not a true superuser. `postgres.py` should work: verify `ADMIN OPTION` on created roles under PG16+ (`createrole_self_grant`), the `REVOKE CONNECT ... FROM PUBLIC` hardening, `ALTER ROLE ... CONNECTION LIMIT`, and the module's existing avoidance of `default_transaction_read_only`.
- TLS: `rds.force_ssl` defaults to 1 on recent PostgreSQL (EST), so `sslmode=verify-ca` with the global bundle is the default path.

### 3.3 Kubernetes layer on EKS (L, 4d)

- **Network policy engine.** Two choices.
  - *VPC CNI network policy* (add-on `enableNetworkPolicy=true`, eBPF, VPC CNI 1.14+/EKS 1.25+, kernel 5.10+; VERIFIED, [EKS docs](https://docs.aws.amazon.com/eks/latest/userguide/cni-network-policy-configure.html)). Supports standard `NetworkPolicy`. I could **not** confirm `ipBlock` with `except` and `endPort` behaviour from the docs, and the docs state a limit of **24 port/protocol combinations per CIDR across all policies**, where each resolved selector counts. Our model (default deny, allow Traefik, egress `0.0.0.0/0` except RFC1918/link-local/reserved, per-app DB CIDR, `endPort` ranges) generates many `except` fragments; this must be tested before committing. Also: policy is applied in parallel with pod start, so pods start **default-allow** unless `NETWORK_POLICY_ENFORCING_MODE=strict` (VERIFIED), and strict mode needs a policy for CoreDNS.
  - *Cilium* replacing or chaining with the VPC CNI (ENI mode). This matches today: **DOKS runs Cilium**, so the existing policies were written and tested against Cilium semantics. It also supplies the bandwidth manager. More operator work (Helm install, upgrades) and not an EKS-managed add-on; Auto Mode cannot do it at all.
  - **Recommendation: Cilium**, unless a one-day VPC CNI spike proves `except`/`endPort` and the 24-combination limit are fine.
- **`kubernetes.io/egress-bandwidth`** (20 Mbit default per pod). The VPC CNI **does** support the annotation via its bandwidth plugin (EKS 1.24+, VPC CNI 1.15+; VERIFIED from the [AWS Containers blog](https://aws.amazon.com/blogs/containers/ensuring-fair-bandwidth-allocation-for-amazon-eks-workloads/)), but that blog states it is **not compatible with VPC CNI network policy** (both use `tc`). So on the VPC CNI path we must choose between policy and bandwidth cap. Cilium gives both. This is the same strategic risk as Azure R3, but with a clearer fix.
- **Storage.** Install the `aws-ebs-csi-driver` add-on (its controller needs a Pod Identity role), create StorageClass `gp3` (`ebs.csi.aws.com`, `reclaimPolicy: Delete`, `volumeBindingMode: WaitForFirstConsumer`, `allowVolumeExpansion: true`), set `VOLUME_STORAGE_CLASS=gp3`. `VOLUMES_MAX_TOTAL` stays 10. **Pin the node group to one subnet/AZ** (EBS is AZ-bound) or a second node in another AZ strands a PVC pod. `bootstrap.yml`'s "Check volume StorageClass" step keeps working with the new name.
- **Ingress / LB.** `deploy/traefik/values.yaml` swaps `service.beta.kubernetes.io/do-loadbalancer-*` for AWS Load Balancer Controller annotations (`aws-load-balancer-type: external`, `nlb-target-type: ip`, `scheme: internet-facing`, `eip-allocations` for stable IPs) or the "no LB" hostPort variant for cheapest viable. The controller is a separate Helm install with an IAM role (Pod Identity) and adds 2 pods against the pod ceiling. Use `externalTrafficPolicy: Local` or PROXY protocol v2 so the login rate limiter sees real client IPs (`config.py` documents the DO LB IP today).
- **Pods per node.** Enable VPC CNI **prefix delegation** (`ENABLE_PREFIX_DELEGATION=true`, set `--max-pods=110` in the node group's kubelet args; EST) or the 17-pod ceiling wrecks capacity. With Cilium ENI IPAM the ceiling is also ENI-bound; prefix mode applies there too.
- **IMDS.** Set `http_tokens=required` and `http_put_response_hop_limit=1` on the node group so pods cannot reach the node role's credentials. Existing tenant policies already except link-local `169.254.0.0/16`, but defence in depth is free.
- **Autoscaler.** `max_nodes` carries over to the managed node group `scaling_config`; the scale-up logic needs Cluster Autoscaler or Karpenter (one more deployment; DOKS built it in). Use `capacity_type=ON_DEMAND`.
- **Upgrades.** Managed node groups do not auto-upgrade; either schedule `update_config`/a recurring CI job or accept manual upgrades. The control plane must stay in standard support (R5).
- **Pods and PSA.** Nothing cloud-specific; EKS supports Pod Security Admission. metrics-server is an EKS add-on (`deploy/metrics-server` becomes a no-op).

### 3.4 Terraform, workflows, DNS, secrets, platform identity (L, 7d)

- **Terraform (`aws`, L):** VPC with two public subnets (nodes, NLB) and two private subnets (RDS, no NAT), S3 gateway endpoint; EKS cluster (standard support, public endpoint restricted by `control_plane_allowed_cidrs`, access entries instead of the legacy aws-auth ConfigMap) and a managed node group (t3a.medium, standard credit mode, one AZ, IMDS hardening, gp3 root); add-ons `vpc-cni`, `kube-proxy`, `coredns`, `eks-pod-identity-agent`, `aws-ebs-csi-driver`, `metrics-server`; RDS MySQL and PostgreSQL instances with parameter groups and subnet group; platform S3 bucket; IAM: GitHub OIDC provider and role, platform Pod Identity role, node role. Keep `prevent_destroy` on cluster and DBs; DO files move to `infra/do/` during the migration. State: TFC keeps working; an S3 backend with a lockfile removes the last long-lived credential.
- **GitHub OIDC (S):** `aws_iam_openid_connect_provider` for `token.actions.githubusercontent.com` plus a role whose trust policy is limited to this repo and `ref:refs/heads/main` (and a separate lower-privilege role for read-only `plan`). `id-token: write` in workflows. Removes `DIGITALOCEAN_TOKEN` and `SPACES_*` secrets; stored values become identifiers (role ARN, region) as repo variables. Only DB master passwords stay secrets.
- **Workflows (M):** `terraform.yml`, `bootstrap.yml`, `deploy.yml`, `diag.yml` swap `doctl` for `aws eks update-kubeconfig`. The labelled bootstrap steps (kubeconfig, LB address, DNS A records, DB connection details, DB CA, VPC CIDR) are rewritten step by step, not as a new flow. The NLB gives a DNS name, so `*` and `lb.<domain>` become CNAMEs (or A records to EIPs); the `CUSTOM_DOMAIN_CNAME_TARGET=lb.<domain>` contract stays stable.
- **DNS and certificates (S):** keep DO DNS and the DO DNS-01 solver for the lowest effort (a scoped DO token remains), or move to Route 53 with cert-manager's Route 53 solver and a role limited to one hosted zone (no static token at all, needs the registrar NS change).
- **Platform identity (M): EKS Pod Identity for the `picopaas-system` service account.** Role scoped to: `iam:CreateUser/DeleteUser/CreateAccessKey/DeleteAccessKey/ListAccessKeys/PutUserPolicy/DeleteUserPolicy/TagUser` on `arn:aws:iam::<acct>:user/picopaas/pp-app-*`, with a **permission boundary** that denies anything outside `s3:*` on `arn:aws:s3:::pp-*`; `s3:CreateBucket/DeleteBucket/PutBucketPublicAccessBlock/ListBucket/GetObject/PutObject/DeleteObject` on `arn:aws:s3:::pp-*`. No EC2/EKS/RDS rights and no static keys. This **solves the "account-wide DO token + full-access Spaces key" hardening item** in `docs/QUESTIONS.md`: the pod can no longer touch anything outside `pp-*` buckets and `pp-app-*` users, and credentials are short-lived and auto-refreshed. A code-execution bug in the API now reaches tenant buckets only. Platform-namespace policies must allow the Pod Identity agent address (`169.254.170.23`, EST), and boto3 needs a Pod-Identity-capable version.
- **Secrets (S):** same k8s Secrets as today (`picopaas-env`, `picopaas-db-ca`); `SPACES_*` disappear. Secrets Manager (~$0.40/secret/mo, EST) is optional and skipped.

### Effort summary

| Block | Effort | Agent-days |
|---|---|---|
| 3.0 Seam refactor and `CLOUD` split (shared with Azure; 0 if already done) | M | 2 |
| 3.1 `S3Store` (IAM per-app creds, presign, quota, moto tests) | M | 3 |
| 3.1 Config only: refs/UI/CLI/examples confirmed unchanged, docs | S | 0.5 |
| 3.2 RDS provider spikes, TLS bundle, parameter groups, networking | M | 2 |
| 3.3 Kubernetes (Cilium or VPC CNI policy tests, bandwidth, storage class, NLB/Traefik, prefix delegation, autoscaler) | L | 4 |
| 3.4 Terraform | L | 4 |
| 3.4 Workflows, OIDC, Pod Identity role, DNS/cert wiring | M | 3 |
| Live-tenant migration tooling and rehearsal (S3-to-S3 copy, DB dumps; no tenant code changes) | M | 2 |
| Docs (infrastructure, runbooks, admin guide, ARCHITECTURE cost section) | S | 1 |
| **Total (EKS)** | | **~21 (19 if phase 0 is already done for Azure)** |

The EC2 + k3s variant (C/D) drops most of 3.3 (EKS add-ons, Auto Mode, node group) and half of Terraform, but adds k3s bootstrap and credential handling:
about **~16 agent-days**.

---

## 4. Risks and unknowns

| # | Risk | Severity | Mitigation |
|---|---|---|---|
| R1 | **Cost goes up** on EKS (+$45 to +$80/mo) because the control plane is $73 and the NLB/IPv4 are billed. | High | Variant C/D (EC2 + k3s), or credits cover it. No amount of tuning gets EKS under about $102 for the bare minimum. |
| R2 | **17 pods per t3a.medium** under VPC CNI defaults; platform pods consume most of them. | High until fixed | Prefix delegation + `--max-pods` (EST, spike), or a bigger node. Verify with a scratch cluster before sizing. |
| R3 | **Egress is metered** ($0.09/GB past the free allowance, allowance EST) while DO bundles transfer. At the 20 Mbit per-pod default one hostile app could push ~6.5 TB/mo (about $580). | High | Cilium bandwidth manager (VPC CNI bandwidth plugin conflicts with VPC CNI policy); AWS Budgets alert (free for 2 budgets, EST); lower `EGRESS_BANDWIDTH_MBIT`; a per-app transfer cap. Lightsail bundles include transfer (E). |
| R4 | `ipBlock except` / `endPort` and the **24 port-protocol combos per CIDR** limit in VPC CNI network policy are unconfirmed for our policy model. | Medium-High | Spike on a scratch cluster; fall back to Cilium. |
| R5 | EKS **extended support +$0.50/h** if the cluster is not upgraded about annually; node groups do not auto-upgrade. | Medium | A calendar item and a CI job; or choose k3s/EC2 where we own the cadence. |
| R6 | RDS micro `max_connections` is about 60 (EST), below DO's ~76; platform pool uses up to 33. | Medium | t4g.small RDS ($25.66 each); keep platform and tenant DBs on separate servers if tenants grow (`docs/research/db-scaling.md`). |
| R7 | EBS volumes are AZ-bound; multi-AZ node groups can strand PVC pods. Reattach is slower than DO. | Low-Medium | One-AZ node group. Accept. |
| R8 | Public IPv4 charge ($3.65 each, node + NLB per AZ) and no NAT: nodes must be in public subnets with locked-down security groups. | Low-Medium | SG allows only 80/443 and the control-plane CIDR; IMDS hop limit 1; accept. |
| R9 | EC2 + k3s: the platform pod needs credentials without Pod Identity (IMDS role with a restricted hop limit exposed to one pod, or a credential refresher). Lightsail has no instance role at all. | Medium | Design in phase 1; for Lightsail use scoped static IAM keys. |
| R10 | IAM eventual consistency delays read-only enforcement by seconds; IAM user limit 5,000 (EST) and 2 keys per user. | Low | Poll interval is minutes; limits are far above 100 apps. |
| R11 | Graviton temptation: a t4g node saves $2.92/mo but runs only arm64 images, breaking amd64 tenants. | Low if avoided | x86 nodes; document why. |
| R12 | Console/`eksctl` defaults enable paid add-ons (control-plane logging, Container Insights). | Low | Terraform only; leave them off. |
| R13 | A scratch AWS stack costs money while it exists; EKS control plane bills from creation ($2.40/day). | Low | Time-box; a request file is required before any paid resource (below). |

**EST items to verify in a spike** (not confirmed in this session): Route 53 hosted-zone fee, free monthly data-transfer allowance, S3 gateway-endpoint
pricing, RDS t4g.micro/small `max_connections` and minimum gp3 size, `rds.force_ssl` and `require_secure_transport` defaults, `mysql.rds_kill`,
PG `rds_superuser` ADMIN-OPTION behaviour, IAM 5,000-user and 2-key limits, VPC CNI `ipBlock except`/`endPort`, prefix-delegation max-pods on t3a, Cilium
ENI-mode bandwidth manager, t3a burstable baseline and credit mode, Auto Mode T-family availability, EKS standard-support window length, NLB LCU at our
traffic, Pod Identity add-on price and agent policy address, Lightsail transfer allowance and DB sizes, free-tier credits.

---

## 5. Phased plan

**Phase 0: seam refactor, DO stays live (2d, $0).** Identical to Azure phase 0 ([azure-port.md section 5](azure-port.md#5-phased-plan)). `CLOUD` config split, `object_store_for()`
factory, the shared S3 data-plane/key-plane split, per-cloud defaults table, contract tests. If the Azure phase 0 lands first, AWS adds only the `S3Store`
shape and the AWS defaults column (about 0.5d). No behaviour change on DO. Land it regardless.

**Phase 1: AWS behind config in a scratch account (~12 agent-days, plus paid scratch infra).** Needs `docs/requests/NNNN-aws-scratch.md`
(format: what / why / cost) and Chas's APPROVED before anything is created. Two shapes to spike, cheapest first:
- *k3s on EC2* (t3a.medium, EIP, RDS MySQL micro, S3, IAM): about $1.60/day without PG, so a two-week spike is about **$22-30** including PG on demand.
- *EKS* (only if EKS is a real candidate): about $5.10/day for shape A, so two weeks is about **$70-85**, mostly the $2.40/day control plane. Scope it to a 3-4 day spike (~$20).
Exit criteria: all of `tests/` pass against moto; a canary app deploys with its own DB and bucket; S3 per-app IAM user can only touch its bucket;
read-only toggle verified; presign with signed `Content-Length` rejects oversize PUT; network policies verified (DB CIDR, `except`, `endPort`, the 24-combo limit);
egress-bandwidth annotation verified; PVC attach/reclaim verified; pods-per-node verified; `terraform validate`/`plan` clean.

**Phase 2: migrate live tenants (2d + a rehearsal).** Same shape as Azure phase 2, with an easier object step:

1. Freeze signups; short maintenance window.
2. DBs: per app, `mysqldump` / `pg_dump` from DO into RDS (50 MB cap, seconds each); recreate roles with new passwords via the providers.
3. **S3-to-S3 copy** (`rclone` or `aws s3 sync` from a Spaces endpoint into each new bucket). **No tenant code changes**, only the injected endpoint/region/keys change on the next reconcile. This removes Azure's "announce before cutover" step.
4. Volumes: copy via a temporary pod (`tar` over `kubectl exec`) or let tenants re-create (not backed up today).
5. Platform DB: dump/restore `picopaas`, flip `DATABASE_URL`.
6. DNS cutover: repoint `*` and `lb.<domain>` (CNAME to the NLB name or A to the EIP). cert-manager reissues the wildcard (unchanged solver if DNS stays on DO).
7. Run `examples.yml` / `quota-e2e.yml` against AWS, then decommission DO after a hold period.

Fallback: keep DO running one week (idle $75/mo); the DNS flip back is one record.

---

## 6. Verdict

- **Effort:** about **20 agent-days** for a complete EKS port (19 if Azure's phase 0 already landed), ~16 for the EC2 + k3s variant. About **3 days** is object storage (Azure: 8), because S3 is native; the contract in `bootstrap.md` is **untouched**.
- **Cost:** **up on EKS, down without it.** EKS like for like about **$155/mo vs $75.30** (+106%); cheapest viable EKS about **$120** ($107 with in-cluster MySQL), and EKS can never go below about $102 because the control plane is $73. EC2 + k3s + RDS + S3 is about **$62** ($48 without PG, $35 with in-cluster MySQL); Lightsail about **$53** ($39 without PG) but with no instance IAM role.
- **Better than Azure on:** storage compatibility, effort, volume pricing ($0.08 vs $0.30 per 1 GiB), no stored-policy/SAS quirks, GitHub OIDC and Pod Identity close the account-wide-token item. **Worse on:** EKS price (control plane $73 vs Azure's free tier), pods-per-node ceiling, extended-support trap.
- **Recommendation:** **do not port for cost alone.** If there is a non-cost reason (credits, tenancy, learning, wanting the platform pod's credentials scoped), land phase 0 (shared with Azure), then spike **k3s on EC2** first (about $25-30). Choose EKS only if credits cover about $155/mo. AWS is the better non-DO destination than Azure on contract risk and effort; it is no cheaper than DO unless we run our own control plane.
- **Questions for Chas** (also add to `docs/QUESTIONS.md` if he wants to pursue this): are there AWS credits or a sponsored account, and for how much per month; is a self-managed k3s node on EC2 acceptable; keep DNS on DO or move to Route 53 (needs a registrar NS change); is closing the account-wide-token hardening item a reason to move on its own?

### Three-way comparison (monthly, USD, list price, 2026-10-23)

| | **DO (today)** | **Azure** (azure-port.md) | **AWS** (this doc) |
|---|---|---|---|
| Like-for-like managed K8s | **$75.30** | $120.80 (AKS Free, D2als_v6) | **$155.24** (EKS $73 + t3a.medium + NLB) |
| Cheapest viable managed K8s | $75.30 | $104.30 (no PG) | $120.18 (no PG, no NLB); $106.84 with in-cluster MySQL |
| Cheapest viable overall (self-run K8s) | n/a | $67.70 (VM + k3s, managed DBs); $51.60 without PG; $37.50 in-cluster MySQL | **$61.96** (EC2 + k3s, RDS); $47.98 without PG; $34.64 in-cluster MySQL; $53.48 Lightsail |
| Object storage | Spaces, S3-compatible, $5, 100-bucket wall | Blob, **not S3** (SAS, tenant code breaks), ~$0.50 | **Native S3**, ~$0.50, 10,000 buckets |
| Contract change (`bootstrap.md`) | none | **required** ("object storage") | **none** |
| Effort (agent-days) | 0 | ~26 | **~20** (EKS), ~16 (EC2 + k3s); 3 is shared phase 0 |
| Egress | pooled allowance, $0.01/GiB over | 100 GB free, $0.087/GB | free allowance (EST), $0.09/GB |
| Volume (1 GiB) | $0.10 | $0.30 | **$0.08** |
| Notable pros | cheapest, simplest, bundled transfer | free AKS control plane, cheap B-series user pool | native S3, OIDC + Pod Identity (no static creds), IAM-scoped platform pod, best volume price |
| Notable cons | account-wide token, 100 buckets, ~22 PG connections | no B-series system pool, mandatory $18 LB, Blob is not S3, 4 data disks per VM | $73 control plane, 17 pods/node default, extended-support cost, bandwidth plugin vs policy conflict |

### Sources

- AWS Price List bulk API, queried 2026-10-23 (`us-east-1`): https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/current/us-east-1/index.json (and `AmazonEKS`, `AmazonRDS`, `AmazonVPC`, `AWSELB`, `AmazonS3`, `AmazonLightsail` at the same path)
- EKS Auto Mode: https://docs.aws.amazon.com/eks/latest/userguide/automode.html
- VPC CNI network policy: https://docs.aws.amazon.com/eks/latest/userguide/cni-network-policy-configure.html and https://docs.aws.amazon.com/eks/latest/userguide/cni-network-policy.html
- VPC CNI bandwidth plugin and its policy conflict: https://aws.amazon.com/blogs/containers/ensuring-fair-bandwidth-allocation-for-amazon-eks-workloads/
- Max pods per instance type: https://github.com/aws/amazon-vpc-cni-k8s/blob/master/misc/eni-max-pods.txt
- EBS volume limits (shared Nitro attachments): https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/volume_limits.html
- RDS quotas and `max_connections` formulas: https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/CHAP_Limits.html
- IAM quotas: https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_iam-quotas.html
- S3 bucket quota (10,000 default, 1M max): https://aws.amazon.com/about-aws/whats-new/2024/11/amazon-s3-up-1-million-buckets-per-aws-account/
- VPC pricing (public IPv4 $0.005/h, $0.09/GB example): https://aws.amazon.com/vpc/pricing/
- EKS Pod Identity (mechanism): https://aws.amazon.com/blogs/containers/amazon-eks-pod-identity-a-new-way-for-applications-on-eks-to-obtain-iam-credentials/
- Free tier changes (secondary): https://builder.aws.com/content/3BlPcDGddaB6WkPvUf1LIy1rzIm/everything-you-need-to-know-to-get-started-with-aws-free-tier
