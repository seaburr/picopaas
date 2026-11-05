# Cloud portability plan: decoupling PicoPaaS from DigitalOcean

Status: **P1 done** (2026-11-01, P1a 684dfcf + P1b d323478/9dd9f71, prod regression clean). P2 (generic target) works locally on Docker Desktop Kubernetes, pushed 2026-11-01 (80dee49, 03a9a66, 0f4e279); kind-in-CI is open. Owner: Chas. Related: [Cloud account checklist](deployment/aws-account-setup.md),
questions in [QUESTIONS.md](QUESTIONS.md) ("Cloud portability").

## Goal

Every place where PicoPaaS talks to DigitalOcean, or relies on DigitalOcean behaviour, goes behind an interface
with a DigitalOcean implementation. Then add **AWS and GCP** implementations, plus a "generic" target (any
Kubernetes cluster, any S3 store, any MySQL or PostgreSQL server). An operator picks one with configuration, not code
changes. The end state is that someone can install PicoPaaS on the cloud they already use.

**Targets (decided 2026-11-01):** AWS and GCP first. Both have object storage that speaks the S3 API: S3 itself, and
GCS through its XML API with HMAC keys. **Azure is deferred** because Blob Storage has no S3 API (see "Later: Azure").

**Which cloud is tested first depends on where free credits are available** (Chas, 2026-11-01). So P1-P3 stay
cloud-neutral, and the generic target (P2) is the proof that the seams are right before any credits are spent.

**Status:** P1 done 2026-11-01. P2 (generic target: Garage + PG + MySQL) runs locally; the kind-based CI e2e is the remaining item (see "P2 status and follow-ups"). P4 (AWS) waits on approval of its test environment.

**Non-goals for now:** moving picopaas.xyz off DigitalOcean, running across several clouds at once, and
spending money on AWS or GCP before a request in `docs/requests/` is approved (see Cost).

## Where we stand

The good news is that the core is already mostly portable:

- **Kubernetes.** The app speaks plain Kubernetes through `KubeProvider` (`app/providers/base.py:115`). Traefik,
  cert-manager, metrics-server and NetworkPolicies all run on any conformant cluster.
- **Databases.** `DbAdmin` (`base.py:229`) is chosen per DbServer by engine (`providers/factory.py`), and it speaks
  the plain MySQL and PostgreSQL protocols. Host, port and admin user come from DbServer rows.
- **Images and env refs.** Image checks go through `Registry` and are not DigitalOcean-specific. The CA-file and
  `db.*` refs are generic too.
- **Platform images.** These live on GHCR, so nothing ties them to DigitalOcean's registry.
- **Backups.** restic already supports S3, GCS (and Azure Blob) natively.

What is coupled to DigitalOcean today:

| # | Area | Where | Coupling | Kind |
|---|------|-------|----------|------|
| 1 | App buckets and keys | `app/providers/spaces.py` (`SpacesStore`), wired in `routers/apps.py:424`, `scheduler.py:71` | The DO API `/v2/spaces/keys` handles per-bucket grants, read-only flips by re-keying ("grants are immutable", `quota.py:111`) and the `*.digitaloceanspaces.com` endpoint | API |
| 2 | Storage readiness check | `routers/auth.py:134` | Hard-coded `DIGITALOCEAN_TOKEN` + `SPACES_*` env | Config |
| 3 | Backup target | `config.py:288,348`, `providers/backup.py` | Default endpoint `https://<region>.digitaloceanspaces.com` | Config |
| 4 | Backup egress | `backups.py:546`, `backup.py:64,282`, `k8s_manifests.py:520-547`, `reconciler.py:137` | Assumes Spaces resolves to private VPC IPs that need a /32 egress hole | Network behaviour |
| 5 | DB host metrics | `app/db_host_metrics.py`, `health.py:81` | DO managed-DB metrics API with `DIGITALOCEAN_TOKEN` | API |
| 6 | Managed-DB quirks | `providers/postgres.py:1-47`, `mysql.py:255,276,365`, `db_tls.py:10` | `doadmin` privileges, admin DB `defaultdb`, port 25060, DO's CA failing `VERIFY_X509_STRICT` | Behaviour |
| 7 | Volumes | `config.py:201-203` | `do-block-storage` default storage class, 1 GiB minimum | Config (already an env var) |
| 8 | Tenant network isolation | `k8s_manifests.py:58-66` | `PRIVATE_CIDRS` reasoning assumes DOKS overlay pod/service CIDRs (10.244/16, 10.245/16) | **Security** |
| 9 | Load balancer | `deploy/traefik/values.yaml:70-79` | `do-loadbalancer-*` annotations | Deploy |
| 10 | DNS | `deploy/dns-sync/dns-sync.yaml` (custom controller on the DO DNS API), `bootstrap.yml:193-203` (`doctl` records) | API | Deploy |
| 11 | TLS DNS-01 | `deploy/cert-manager/clusterissuer.yaml` | DigitalOcean solver + DO token secret | Deploy |
| 12 | Cluster access in CI | `deploy.yml:43,97-107`, `bootstrap.yml:83-203`, `diag.yml`, `terraform.yml:52-70` | `doctl` kubeconfig, LB cleanup, DO API checks | CI |
| 13 | Infrastructure | `infra/*.tf` | DO provider only (DOKS, managed MySQL/PG, Spaces, VPC, firewall, DNS) | IaC |
| 14 | In-cluster MySQL backups | `deploy/mysql/backup-cronjob.yaml` | Spaces endpoint | Deploy |
| 15 | Docs and copy | `deploy/README.md`, `infra/README.md`, `ADMIN_GUIDE.md`, comments | Written for DO | Docs |

## Design

### 1. One switch: `CLOUD_PROVIDER`

`CLOUD_PROVIDER = digitalocean | aws | gcp | generic` (default `digitalocean`, so today's deployment is
unchanged). A single module, `app/providers/cloud.py`, builds each provider from config. Routers, the scheduler and
health checks ask that module for providers and never import an implementation directly. That removes the direct
`SpacesStore` imports in `routers/apps.py` and `scheduler.py`.

### 2. Interfaces (application layer)

| Interface | Methods (shape) | digitalocean | aws | gcp | generic |
|-----------|-----------------|--------------|-----|-----|---------|
| `ObjectStore` (exists; tidy it) | `create_bucket_and_key`, `rotate_key`, `set_read_only`, `presign`, `usage`, `delete` ... | Spaces + DO keys API (today) | S3 + one IAM user per app, bucket-scoped policy; read-only = swap the policy (no re-key) | GCS bucket + one service account per app with an HMAC key, bucket IAM binding; read-only = swap `objectAdmin` for `objectViewer`; S3 calls go to `storage.googleapis.com` | Garage (admin API: per-key, per-bucket read/write grants and bucket quotas, the closest match to the Spaces key model); SeaweedFS as fallback |
| `BackupTarget` (new, small) | `restic_repository_url(app)`, `env()`, `egress_rule()` | Spaces S3 URL, private-IP egress | `s3:` URL, endpoint egress (VPC endpoint optional) | `gs:` restic backend (workload identity) or S3 interop | any S3 URL |
| `DbHostMetrics` (new, optional) | `disk_bytes()`, `cpu()`, `connections()` or `None` | DO metrics API | CloudWatch (RDS) | Cloud Monitoring (Cloud SQL) | none (Health page shows data size only, as today when unset) |
| `DbFlavor` (new, data not code) | admin DB name, CA bundle source, strict-X509 workaround, privilege caveats, default port | `defaultdb`, 25060, DO CA | `postgres`/`mysql`, RDS global bundle | `postgres`/`mysql`, per-instance Cloud SQL server CA | plain values |
| `StorageReadiness` | `storage_configured()` | today's env check | provider check | provider check | provider check |

Rules:
- An interface is drawn around **what the platform needs**, not around a cloud API. If a capability is missing,
  the method returns "unsupported" and the UI hides the feature, the way storage already degrades when unset
  (`scheduler.py:78`).
- **Env vars for apps stay the same on every cloud.** Apps keep seeing `DATABASE_URL`, `DB_*`, `S3_*` and the
  `${{ db.* }}` / `${{ storage.* }}` refs, so templates and Kaisen output stay portable. This is why every
  first target must offer an S3 API.
- Config renames keep the old names as aliases (`SPACES_*` -> `OBJECT_STORE_*`, `DIGITALOCEAN_TOKEN` stays
  DO-only).
- DbServer rows gain a `flavor` column (`do-managed | rds | cloudsql | self-managed`) that selects the
  `DbFlavor` values. The default is `do-managed`, so existing rows don't change behaviour.

### 3. Tenant isolation must be re-proved per cloud (blocking for AWS/GCP)

Today's NetworkPolicies deny private ranges and allow the internet, which is safe on DOKS because pods sit on an
overlay. On **EKS with the default VPC CNI, pods get real VPC IPs**, and NetworkPolicy is only enforced if the
VPC CNI network-policy agent (or Calico) is enabled. **GKE** pods are VPC-native (alias IPs) too, and
NetworkPolicy needs Dataplane V2 (Cilium) or the Calico add-on. Each cloud target therefore:
- pins a CNI and policy engine in its Terraform module (EKS: VPC CNI with network policy on, or Calico; GKE:
  Dataplane V2);
- blocks the instance metadata endpoint (169.254.169.254) and the cloud's DNS/link-local services explicitly.
  Metadata can hand out node credentials. On AWS, require IMDSv2 with hop limit 1. On GKE, use Workload
  Identity with the GKE metadata server, and give tenant pods no Google service account;
- gets a security-reviewer pentest (the existing K-series checks: cross-tenant, platform DB, metadata, egress)
  before any user app runs there.

`PRIVATE_CIDRS` and the DB egress CIDRs become per-cloud config generated from Terraform outputs, not constants.

### 4. Deploy layer

- **Packaging.** Turn `deploy/` into a Helm chart (or Kustomize base) for the platform, Traefik values,
  cert-manager issuers and metrics-server, with overlays `digitalocean/`, `aws/`, `gcp/` and `generic/`.
  Overlays carry only LB annotations, storage class, issuer solver and CIDRs.
- **DNS.** Replace the custom `dns-sync` controller with **external-dns**, which supports DigitalOcean, Route 53
  and Google Cloud DNS. Check first that it covers what dns-sync does for custom domains; if not, put dns-sync
  behind a `DnsProvider` with the same three implementations.
- **TLS.** cert-manager already has DigitalOcean, Route 53 and Cloud DNS DNS-01 solvers, so this is
  configuration only.
- **Load balancer.** Traefik Service annotations per overlay: DO LB, AWS NLB (`aws-load-balancer-type: nlb`,
  client IP preserved), GKE external passthrough network LB (`externalTrafficPolicy: Local`). The
  proxy-protocol setting is checked per cloud.
- **Storage class.** `gp3` (EBS CSI) on AWS and `standard-rwo` (PD CSI) on GKE, both with reclaimPolicy
  Delete. Both have a 1 GiB minimum, so the volume sizes stay as they are.
- **CI.** The deploy workflow gets a `cluster-login` composite action per cloud: `doctl`, then
  `aws eks update-kubeconfig` through GitHub OIDC with an assumed role, or `gcloud container clusters
  get-credentials` through Workload Identity Federation. No long-lived cloud keys in GitHub. LB cleanup and DNS steps in `bootstrap.yml` go to
  Terraform or external-dns.

### 5. Infrastructure as code

- `infra/` becomes `infra/modules/{digitalocean,aws,gcp}` behind one **output contract**: cluster name and
  kubeconfig method, DB servers (engine, host, port, admin secret ref, CA), object-store endpoint and admin
  credentials ref, backup target, pod/service/VPC CIDRs, DNS zone, storage class.
- `bootstrap` reads that contract, so the chart and the app never need to know which module ran.
- The equivalents:
  - **AWS:** EKS + RDS MySQL/PostgreSQL + S3 + Route 53 + NLB.
  - **GCP:** GKE (zonal, Dataplane V2) + Cloud SQL MySQL/PostgreSQL (private IP) + GCS + Cloud DNS + network LB.
- **Generic** has no Terraform. The operator brings a cluster, an S3-compatible store and DB servers, and fills
  in the contract values by hand.

### 6. Testing

- **Contract tests.** One pytest suite per interface runs against the fake and against every real implementation
  when that provider's env is present. This is the same idea as today's fakes, but shared.
- **A generic target in CI, at no cloud cost.** kind + Garage + PostgreSQL + MySQL containers in GitHub
  Actions, running the end-to-end smoke (deploy an app, DB, bucket, backup, restore, quota read-only). This
  proves "runs anywhere" on every PR and is the cheapest way to keep the abstraction honest.
- **Real-cloud runs** happen only on demand (before a release or after a provider change), on a short-lived
  environment that Terraform creates and destroys in the same workflow.

**No MinIO** (Chas, 2026-11-01: the project is dead). Use Garage first, SeaweedFS if Garage falls short.

## P2 status and follow-ups (2026-11-01)

P2a (GarageStore, Garage v2.5.0, admin API v2), P2a.2 (generic backups: endpoint port, path-style, region), P2b
(`deploy/generic/` + `scripts/dev/local-up.sh` on Docker Desktop Kubernetes, which is kind-based with kindnet and
enforces NetworkPolicy), and P2c (in-cluster egress by namespace/pod selector, PG16 SET ROLE grant, BOOTSTRAP_*_FLAVOR)
are done locally. Live local checks (5 tries each) passed: own DB and S3 open; Garage admin and rpc, the other engine's
DB and metadata blocked; internet open; backup of db + bucket and export OK.

Open follow-ups:
1. **Public object-store endpoint.** Presigned and backup-download URLs use the in-cluster Garage URL, so browsers
   outside the cluster can't open them. Add `OBJECT_STORE_PUBLIC_ENDPOINT` (Garage behind an Ingress) for presigns
   and downloads, keeping the internal URL for apps and the platform.
2. **`deploy/platform/backup.yaml` in-cluster variant.** Its `allow-egress-db` is CIDR-only. Locally it's
   overridden by `deploy/generic/platform/backup-db-egress.yaml`; fold this into the Helm chart (P3).
3. **Multi-arch images.** `ghcr.io/seaburr/picopaas-backup` is amd64 only, and so may be the platform image. Build
   linux/arm64 too, for Apple Silicon and Graviton/Ampere self-hosters.
4. **cryptography on arm64.** 50.0.2 crashed with SIGILL on Docker Desktop engine 27.5.1's VM but not on 29.9.2.
   Keep the opt-in `ARM64_CRYPTO_FIX=1` overlay and re-test before the self-install guide.
5. **CI.** Run `local-up.sh` on kind in GitHub Actions plus an e2e smoke (the remaining P2 item).

## Phases

Each phase is shippable and leaves picopaas.xyz on DigitalOcean working unchanged.

| Phase | What | Who | Done when |
|-------|------|-----|-----------|
| **P0 Decide** | Answer Q1 to Q5 below; approve this plan | Chas | Answers in QUESTIONS.md |
| **P1 Seams (no behaviour change)** | `CLOUD_PROVIDER` + `cloud.py`; route all provider construction through it; `BackupTarget`, `DbHostMetrics`, `DbFlavor`, `StorageReadiness`; config aliases; `flavor` column + migration; CIDRs from config | backend-dev | Full test suite green; prod deploy byte-identical in behaviour; `grep -r digitalocean app/` hits only `providers/digitalocean/` and config defaults |
| **P2 Generic target** | Garage `ObjectStore`, generic flavor, Helm chart + `generic` overlay, kind-based e2e in CI | backend-dev, infra-ops, qa-tester | A fresh kind cluster runs the full smoke in CI |
| **P3 Deploy portability** | external-dns replaces dns-sync on DO; cert-manager and Traefik via overlays; per-cloud cluster-login action; Terraform module + output contract for DO | infra-ops, security-reviewer | prod redeployed from the chart with no user-visible change |
| **P4 AWS** | S3/IAM `ObjectStore`, RDS flavor, CloudWatch metrics, EKS module (CNI + IMDS hardening), NLB overlay | backend-dev, infra-ops, security-reviewer | Pentest on a throwaway EKS env passes; smoke green; env destroyed |
| **P5 GCP** | GCS/HMAC `ObjectStore`, Cloud SQL flavor, Cloud Monitoring metrics, GKE module (Dataplane V2, Workload Identity, metadata hardening) | same | Same as P4 |
| **P6 Self-install** | "Install PicoPaaS on your cloud" guide; one `values.yaml` per cloud; upgrade notes | tech-writer | A new operator can follow it end to end |

Rough size: P1 and P2 are the bulk of the code work; P4 and P5 are mostly Terraform plus one new
`ObjectStore` each.

## Cost

P1 to P3 cost nothing extra (the generic target runs in GitHub Actions). P4 and P5 need short-lived cloud
accounts:
- **EKS:** the control plane alone is about $0.10/hour (~$73/month if left up), so it must be created and
  destroyed per test run.
- **GKE:** the free tier credit covers one zonal (or Autopilot) cluster's management fee, so the control plane
  is effectively free. Nodes, Cloud SQL and the LB are not.

Each needs a request in `docs/requests/` with a per-run estimate and a hard teardown before anything is
provisioned.

## Later: Azure

Deferred (Chas, 2026-11-01) because Blob Storage has no S3 API. When it comes back, the choice is:
- **(a) native Blob:** apps get `AZURE_STORAGE_*` + SAS refs, and templates that need S3 won't work;
- **(b) an S3 layer** (in-cluster Garage or SeaweedFS on a disk): apps stay identical, but it's one more thing to run.

The P2 generic target already builds (b)'s Garage `ObjectStore`, so Azure later is mostly AKS (Azure CNI
Overlay + Cilium), Flexible Server, Azure DNS and Terraform. restic backs up to Azure Blob natively.

## Questions for Chas (also in QUESTIONS.md)

1. **AWS per-app credentials.** Options:
   - IAM users with access keys: closest to today. The default IAM user quota is 5,000, which is fine.
   - Shared credentials with prefix-scoped policies: fewer moving parts, weaker isolation.

   Recommend IAM users per app.
2. **GCP per-app credentials.** HMAC keys belong to service accounts. The default quota is 100 service
   accounts per project (raisable), with up to 10 HMAC keys each. Options:
   - one service account per app: clean isolation, but needs a quota raise past ~100 apps;
   - a project per installation with a raised quota.

   Recommend a service account per app and documenting the quota.
3. **Which test accounts?** Whose AWS and GCP accounts host the P4/P5 throwaway environments, and what
   per-run spend cap applies?
4. **Order.** AWS before GCP? (Recommended: the S3 path is closest to today's code. GCP then mostly adds HMAC
   key management.)
5. **Packaging.** Is Helm acceptable as the install format for self-hosters? (Recommended: yes; Kustomize
   overlays are fine internally.)
