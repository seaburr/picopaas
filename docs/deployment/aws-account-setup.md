# AWS account setup for PicoPaaS P4 testing (operator checklist)

Status: PROPOSED (request [0010](../requests/0010-aws-test-environment.md), awaiting Chas). Nothing here has been done.
Context: [PORTABILITY_PLAN.md](../PORTABILITY_PLAN.md) phase P4, [research/aws-port.md](../research/aws-port.md). Prices checked 2026-11-02
(sources at the bottom). Every step is **Chas** (only he can, or it needs his login) or **Agent** (infra-ops, after the matching
request is APPROVED). Agents never run AWS commands against the account before step 14's gate and an APPROVED 0010.

Design in one paragraph: a dedicated **sandbox member account** under a new AWS Organization, no long-lived keys anywhere
(Identity Center for people, GitHub OIDC for CI), Terraform state in S3 with a native lockfile, one persistent Route 53 zone
for `aws.picopaas.xyz`, everything else created and destroyed per run. Idle cost is about $0.51/mo.

## 0. Decisions first (Chas, in QUESTIONS.md)
Whose account and credits, region, per-run and monthly caps, and whether spot nodes are acceptable. Defaults below assume:
**new account, us-east-1, $5 per run, $25 per month, on-demand nodes first.**

## Part A. Account, people, money (Chas)

1. **Create the AWS account.** Use a dedicated mailbox/alias (e.g. `aws-root@<your domain>`), not a personal login.
   New accounts since July 2025 get $100 credits at sign-up and up to $100 more for exploring services, valid 6 months; they start
   on the **Free plan**, which is limited to select services and gets no promotional credits.
   - Upgrade to the **Paid plan** (Billing console, "Upgrade plan") before using EKS; remaining credits carry over.
   - Creating an Organization (step 3) also upgrades you to Paid; **do the upgrade first on purpose**: joining an existing
     Organization from a Free-plan account expires its credits immediately.
   - Credit-card required. Alternatives (ask in QUESTIONS): an employer/other account that already has credits, or AWS Activate
     (Founders $1,000 for self-funded pre-Series-B startups with a Paid-plan account; check the current terms yourself).
   - Done when: you can sign in to the console and Billing shows Plan = Paid and the credit balance.
2. **Lock down the root user.** Console > Security credentials: enable MFA with two registered passkeys/security keys;
   confirm **no root access keys**; set alternate billing/security/operations contacts. Store the root login in the password
   manager and do not use it again except for account-level tasks.
   - Done when: the root "Security credentials" page shows MFA assigned and 0 access keys.
3. **Create the Organization and a sandbox member account.** AWS Organizations > Create organization (all features); then
   "Add an AWS account > Create": name `picopaas-sandbox`, email alias `aws-sandbox@...`, role `OrganizationAccountAccessRole`.
   Why an Organization: IAM Identity Center permission sets (SSO into accounts) need an organization instance, and an SCP gives a
   second kill switch. The management account then holds **nothing** but billing, Identity Center and Budgets.
   Credits stay on the paying (management) account and apply across the org (verify on the first invoice; EST).
   - Done when: both accounts are listed and the sandbox account ID is noted (not secret, but not committed to docs).
4. **IAM Identity Center.** In the management account, region us-east-1: Enable IAM Identity Center (organization instance,
   no charge). Create user `chas`, MFA required; create permission set `SandboxAdmin` (AdministratorAccess, 4 h session);
   assign user + set to `picopaas-sandbox` only. Configure the CLI with `aws configure sso`.
   - Done when: `aws sts get-caller-identity --profile picopaas-sandbox` returns the sandbox account ID.
5. **Billing guardrails** (management account, Billing and Cost Management). All free for basic alerts; budget *actions* are free
   for the first two budgets.
   - Billing preferences: tick "Receive Free Tier usage alerts", "Receive billing alerts", "IAM user and role access to Billing".
   - Budget 1, monthly cost budget `picopaas-monthly`, **$25**, linked account = sandbox. Alerts (actual) at 20% ($5), 50%, 80%, and
     100% forecasted, to your email.
   - Budget 2, **daily** cost budget `picopaas-daily`, **$8**, alerts at $3 and $6. One forgotten EKS stack is about $6.50/day.
   - Cost Anomaly Detection: monitor type "AWS services", alert subscription with an absolute threshold of **$5**, email.
   - Cost allocation tags: activate user tags `project`, `env`, `ttl` (they appear 24 h after first use).
   - Done when: all three alert types show "OK" and a test email has arrived (use "Send test notification" where offered).
6. **Kill switch (backstop, not a hard cap).** Budget data lags 8-24 h, so alerts are early warnings only. Layers, cheapest first:
   - (a) **Low quotas**: keep the EC2 vCPU quotas near what a run needs (step 9). No instances beyond that can start.
   - (b) **Budget action** on `picopaas-monthly` at 100% actual: attach an IAM deny policy `DenyNewSpend` (deny
     `eks:CreateCluster`, `ec2:RunInstances`, `ec2:CreateNatGateway`, `ec2:AllocateAddress`, `rds:CreateDBInstance`,
     `elasticloadbalancing:CreateLoadBalancer`) to the Terraform and deploy roles (step 8). Deny never blocks deletes, so teardown works.
     Set approval to **automatic**. Org alternative: the same list as an SCP on the sandbox account.
   - (c) **Nightly teardown** workflow (Agent, P4 deliverable): fails loudly if any resource tagged `project=picopaas` exists past
     its `ttl`, and a manual "destroy" dispatch exists.
   - Done when: the policy exists, the action shows "Standby", and you've read the "nothing left billing" list (Part C).
7. **Region.** Use **us-east-1** for everything (pin it via an SCP `aws:RequestedRegion` allow-list of us-east-1 plus global
   services, and keep a single region in the Terraform variable). Justification: EKS, RDS and NLB prices are identical in
   us-east-2 for everything we use (EST for Ohio; check the calculator), us-east-1 is the baseline of our research prices, has the
   deepest spot/instance capacity and gets new features first; its downside is occasional wider outages, which a throwaway env tolerates.
   Choose us-east-2 only if you prefer its lower blast radius. It is a one-variable change. Don't mix regions.
   - Done when: Terraform `AWS_REGION` variable and the SCP agree.

## Part B. CI identity, state, DNS (Chas creates the bootstrap once, Agent owns the rest)

The first resources (OIDC provider, state bucket, roles) are a one-time **manual bootstrap** by Chas in the sandbox account,
because Terraform cannot create the thing it logs in with. Agents later codify them in `infra/aws/bootstrap/` (import, not recreate).

8. **GitHub OIDC provider and two roles** (sandbox account, Chas; or Agent writes the CLI/Terraform, Chas runs it).
   - IAM > Identity providers > Add > OpenID Connect: URL `https://token.actions.githubusercontent.com`, audience `sts.amazonaws.com`.
   - Role `picopaas-p4-terraform` (create/destroy the test env). Trust: federated principal above, conditions
     `aud = sts.amazonaws.com` and `sub = repo:seaburr/picopaas:environment:aws-p4-apply` (StringEquals). Permissions: start with
     AdministratorAccess **inside the sandbox account** plus a permissions boundary that denies IAM-user/Organizations/Billing/
     account changes and anything outside us-east-1; the agent proposes a tighter policy after the first successful run.
   - Role `picopaas-p4-deploy` (kubeconfig + roll out the platform). Trust: `sub = repo:seaburr/picopaas:environment:aws-p4-test`.
     Permissions: `eks:DescribeCluster`, `eks:ListClusters` only; cluster access is a Terraform-created EKS **access entry** mapping this
     role to a namespaced Kubernetes group (no cluster-admin from CI beyond what deploy needs).
   - Create GitHub **Environments** `aws-p4-apply` (required reviewer = Chas, restrict to branch `main`) and `aws-p4-test`.
     The environment is the human gate; the trust policy ties each role to one environment of this repo only.
   - Set `max session duration` 1 h. Workflows need `permissions: id-token: write` (same style as the existing `deploy.yml`/`terraform.yml`,
     which today use DO tokens and Terraform Cloud state).
   - Caveat: repos created after 2026-07-15 or opted in use ID-based `sub` values (`repo:<org>@<id>/<repo>@<id>:...`). Agent adds a debug step
     that prints the actual `sub` on the first run; fix the trust policy if it differs.
   - Done when: a workflow_dispatch "aws-whoami" run in environment `aws-p4-test` prints the deploy role ARN, and the same run
     from a non-approved branch/environment is **denied**.
9. **Service quota checks** (Agent, read-only; Chas for the raise requests). New accounts can start with very low quotas.
   ```bash
   aws service-quotas get-service-quota --service-code ec2 --quota-code L-1216C47A   # Running On-Demand Standard vCPUs: need >= 8
   aws service-quotas get-service-quota --service-code ec2 --quota-code L-34B43A08   # All Standard Spot vCPUs (if spot)
   aws service-quotas get-service-quota --service-code ec2 --quota-code L-0263D0A3   # EIPs (default 5; need ~4)
   aws service-quotas get-service-quota --service-code vpc --quota-code L-F678F1CE   # VPCs per region (default 5; need 1)
   aws service-quotas get-service-quota --service-code eks --quota-code L-1194D53C   # EKS clusters (default 100; need 1)
   ```
   (Verify the quota codes with `list-service-quotas` first.) Need: 8 vCPU covers 2 x t3a.medium plus one surge node.
   Do **not** raise above ~8 to keep (a) of step 6 meaningful; request a raise only if the default is lower.
   - Done when: all needed quotas show the required values.
10. **State backend.** Bucket `picopaas-tfstate-<sandbox-account-id>` in us-east-1, created manually: versioning on, SSE-S3 (or KMS) default
    encryption, Block Public Access all on, bucket policy allowing only the two Terraform roles + `chas`. State uses the S3 backend's native
    lockfile (`use_lockfile = true`, Terraform 1.10+); no DynamoDB table (the DynamoDB lock is deprecated). The `.tflock` object needs
    Get/Put/DeleteObject. Lifecycle: expire noncurrent versions after 30 days. Cost: cents. Keep it outside the Terraform stack that gets destroyed.
    - Done when: `terraform init` with the backend block succeeds from the `aws-p4-apply` environment and `terraform plan` on an empty stack is clean.
11. **Tagging convention** (Agent: provider `default_tags`; Chas: SCP/tag policy optional). Every resource:
    `project=picopaas`, `env=p4-test`, `ttl=<YYYY-MM-DD>` (default today+1), `owner=chas`, `managed-by=terraform`.
    Teardown and the nightly check key off `project` + `ttl`. Resources that don't take tags (a few IAM items) get the `pp-` prefix.
12. **DNS delegation** of `aws.picopaas.xyz` (prod DNS at DO stays untouched except one NS record).
    - Chas (sandbox account): `aws route53 create-hosted-zone --name aws.picopaas.xyz --caller-reference p4-$(date +%s)`; note the
      zone ID and the four `ns-*.awsdns-*` name servers. Create it once and **never delete it** so the delegation stays valid ($0.50/mo,
      not prorated; deleting within 12 h is free but the NS set changes on re-create).
    - Chas (DO): add four `NS` records on `picopaas.xyz` with host `aws`, one per name server (control panel > Networking > Domains,
      or `doctl compute domain records create picopaas.xyz --record-type NS --record-name aws --record-data ns-XXXX.awsdns-XX.org.`).
      An explicit NS delegation beats the existing wildcard record; `*.aws.picopaas.xyz` and `lb.aws.picopaas.xyz` are then Route 53's.
    - Agent later: Terraform reads the zone as a **data source** (`AWS_DNS_ZONE_ID`), external-dns/cert-manager Route 53 solver get a role
      scoped to this one zone.
    - Done when: `dig NS aws.picopaas.xyz +short` returns the AWS servers and a TXT record created in the zone resolves publicly.
13. **Registry: ECR not needed.** The platform image and tenant test images are public on GHCR; nodes sit in public subnets and pull
    directly. Chas to confirm `ghcr.io/seaburr/picopaas` is anonymously pullable. (Docker Hub anonymous pulls from shared AWS IPs may
    hit rate limits; the P4 tests use GHCR images.)

### EKS hardening prerequisites (from PORTABILITY_PLAN section 3)
Mostly Terraform deliverables of P4 (Agent), checked at the gate after the first apply, not account steps:
- **IMDSv2, hop limit 1** on node launch templates (`http_tokens=required`, `http_put_response_hop_limit=1`). Account-wide default
  as a free safety net (Chas or Agent, once): `aws ec2 modify-instance-metadata-defaults --http-tokens required --http-put-response-hop-limit 1 --region us-east-1`.
- **NetworkPolicy-capable CNI** enabled and proved: VPC CNI with `enableNetworkPolicy=true` (EKS >= 1.25, VPC CNI >= 1.14, eBPF) or Cilium. The
  default VPC CNI does **not** enforce policy. Open spike: `ipBlock except`, `endPort`, the 24-combination limit (research R4).
- Pods can't reach 169.254.169.254 (tenant pod curl test), nodes run in public subnets with SGs allowing only 80/443 from the NLB and the
  control-plane CIDR, control-plane logging off (paid), no Container Insights, add-ons limited to vpc-cni, kube-proxy, coredns, ebs-csi, metrics-server.
- The security-reviewer runs the K-series pentest (cross-tenant, platform DB, metadata, egress) before any real user app runs.

### GitHub repo configuration (Chas; names only, no values here)
Repository **variables** (not secrets; scope them to the environments where noted):
`AWS_REGION`, `AWS_ACCOUNT_ID`, `AWS_TF_ROLE_ARN` (env `aws-p4-apply`), `AWS_DEPLOY_ROLE_ARN` (env `aws-p4-test`), `AWS_TFSTATE_BUCKET`,
`AWS_DNS_ZONE_ID`, `AWS_DNS_DOMAIN` (`aws.picopaas.xyz`).
Repository **secrets**: none for AWS (OIDC replaces keys). Per-run platform secrets (`SECRET_KEY`, `FERNET_KEY`, DB master passwords) are generated by
Terraform/bootstrap into Kubernetes Secrets, as on DO. Do not create `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`.

## Part C. Teardown, verification, gate

### Teardown (Agent, per run; manual dispatch or nightly)
1. Delete tenant apps through the API (frees buckets, IAM users `pp-app-*`, PVC-backed EBS volumes), then `kubectl delete svc -n traefik` /
   LB Services and PVCs so the controller removes the NLB and volumes **before** the cluster goes.
2. `terraform destroy` (RDS: `skip_final_snapshot = true`, no deletion protection in the p4 env; S3 buckets `force_destroy`).
3. Do **not** destroy: the state bucket, OIDC provider, roles, the Route 53 zone.

### "Nothing left billing" verification (Agent runs, Chas spot-checks; empty output = pass)
```bash
aws eks list-clusters
aws rds describe-db-instances --query 'DBInstances[].DBInstanceIdentifier'
aws rds describe-db-snapshots --snapshot-type manual --query 'DBSnapshots[].DBSnapshotIdentifier'
aws ec2 describe-nat-gateways --filter Name=state,Values=available,pending
aws ec2 describe-addresses --query 'Addresses[].PublicIp'                 # idle public IPv4 bills $0.005/h each
aws ec2 describe-volumes --query 'Volumes[].VolumeId'                      # orphaned PVC volumes
aws ec2 describe-instances --filters Name=instance-state-name,Values=running,pending,stopping
aws elbv2 describe-load-balancers --query 'LoadBalancers[].LoadBalancerName'
aws logs describe-log-groups --query 'logGroups[].logGroupName'
aws s3api list-buckets --query 'Buckets[].Name'                            # only tfstate bucket expected
aws iam list-users --path-prefix /picopaas/                                # pp-app-* users
aws resourcegroupstaggingapi get-resources --tag-filters Key=project,Values=picopaas
```
Next day: Cost Explorer (group by service, filter tag `project=picopaas`) shows $0/day; the daily budget stays under $0.05.
Expected idle residue: Route 53 zone ($0.50/mo), state bucket (cents), the OIDC provider and roles ($0).

### 14. Ready-for-P4 gate (Chas ticks; Agent then starts P4 after 0010 is APPROVED)
- [ ] Paid plan + credits visible; root MFA, no root keys; Organization + `picopaas-sandbox` exist
- [ ] Identity Center user can reach the sandbox; budgets (monthly, daily), anomaly alert, `DenyNewSpend` action in place
- [ ] Region fixed (us-east-1); quotas verified (vCPU >= 8, EIPs, VPC, EKS)
- [ ] OIDC provider + `picopaas-p4-terraform` / `picopaas-p4-deploy` roles; whoami run passes from the right environment, is denied elsewhere
- [ ] State bucket (versioning, encryption, lockfile) works; `aws.picopaas.xyz` delegated and `dig` confirms
- [ ] GitHub variables listed above set; environments `aws-p4-apply` (reviewer) and `aws-p4-test` exist
- [ ] Per-run and monthly caps decided in QUESTIONS.md; request 0010 marked APPROVED
- [ ] Teardown list reviewed; GHCR images confirmed public

## Cost (list prices, us-east-1, 2026-11-02)
Per hour of a running test env (EKS + 2 x t3a.medium + NLB + RDS MySQL micro + RDS PG micro + 20 GiB gp3 each + S3 + Route 53), nodes in
**public subnets, no NAT**:

| Line | $/hour | Note |
|---|---|---|
| EKS control plane | 0.100 | standard support; extended support is 0.60/h, stay on a supported version |
| 2 x t3a.medium on-demand | 0.075 | about 0.0376 each (research); spot roughly 60-70% less (EST) |
| 2 x 20 GiB gp3 node disks | 0.004 | 0.08/GiB-mo (EST) |
| NLB | 0.030 | 0.0225/h + about 0.006/NLCU |
| Public IPv4 (2 nodes + NLB, about 4) | 0.020 | 0.005/h each, in-use or idle |
| RDS db.t4g.micro MySQL + PG, single-AZ | 0.032 | 0.016/h each (third-party mirror of list price) |
| RDS storage 2 x 20 GiB gp3 | 0.006 | about 0.115/GiB-mo (EST); 20 GiB minimum |
| S3, Route 53 queries, data out | about 0 | first 100 GB/mo out free; requests fractions of a cent |
| **Total** | **about 0.27/h, 6.4/day** | |

- With one NAT gateway (private nodes): +0.045/h + 0.045/GB, about 0.31/h. **Not recommended**; costs 1/6 more with no test value
  (but the S3 gateway endpoint is free, add it anyway).
- Spot nodes: about 0.22/h (EST). Fine for tests except the PVC/AZ-pinned scenarios; keep on-demand for the pentest run.
- A realistic run (apply 25 min, tests 3 h, destroy 15 min) is about 4 h = **about $1.10**; a long pentest day of 10 h is **about $2.70**.
  The $100-$200 new-account credits cover roughly 370+ run-hours.
- Recommended caps: **$5 per run** (about 18 h of a stack; the nightly teardown enforces it), **$25 per month** (about 4-6 runs plus slack), daily budget $8.
- **Always-on when idle: about $0.51/mo** = Route 53 hosted zone $0.50 + state bucket and its versions (cents). OIDC, IAM, Identity Center, Organizations,
  Budgets (first two actions) and anomaly detection are free. If a run is forgotten: $6.40/day, which the $8 daily budget and the $5 anomaly alert will catch within a day.
- Traps: the RDS "stopped" state restarts after 7 days; destroyed clusters leave EBS volumes, NLB ENIs and log groups if apps weren't deleted first (checklist above);
  CloudWatch control-plane logging and Container Insights are paid and left off.

## Sources (checked 2026-11-02)
- EKS pricing: https://aws.amazon.com/eks/pricing/ ($0.10/h standard, $0.60 extended)
- VPC pricing (NAT $0.045/h + $0.045/GB, public IPv4 $0.005/h, S3 gateway endpoint free; page shows Ohio rates): https://aws.amazon.com/vpc/pricing/
- NLB pricing ($0.0225/h + $0.006/NLCU, us-east-1): https://aws.amazon.com/elasticloadbalancing/pricing/
- Route 53 ($0.50/zone/mo, $0.40/M queries, free delete within 12 h): https://aws.amazon.com/route53/pricing/
- RDS (page tables did not load; db.t4g.micro $0.016/h from https://instances.vantage.sh/aws/rds/db.t4g.micro, gp3 rate unverified; 20 GiB minimum): https://aws.amazon.com/rds/mysql/pricing/
- S3 ($0.023/GB-mo first 50 TB, third-party, page tables did not load): https://aws.amazon.com/s3/pricing/
- Free plan, credits, 6-month limit, Paid plan: https://aws.amazon.com/free/ and https://aws.amazon.com/blogs/aws/aws-free-tier-update-new-customers-can-get-started-and-explore-aws-with-up-to-200-in-credits/ ; EKS only within credits: https://repost.aws/questions/QUiBBWhYPIQS6Zkq_awLIeyg/
- Activate: https://aws.amazon.com/aws-startups-1/startups/credits/
- Identity Center instances: https://docs.aws.amazon.com/singlesignon/latest/userguide/identity-center-instances.html
- GitHub OIDC in AWS: https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws
- EKS quotas (clusters 100, node groups 30): https://docs.aws.amazon.com/general/latest/gr/eks.html
- Terraform S3 backend `use_lockfile`: https://developer.hashicorp.com/terraform/language/backend/s3
- Budgets actions (IAM policy / SCP): https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-controls.html
