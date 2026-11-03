# 0010: AWS throwaway test environment for portability P4 (status: PROPOSED, awaiting Chas)

Operator steps: `docs/deployment/aws-account-setup.md`. Plan: `docs/PORTABILITY_PLAN.md` phase P4. Nothing here is created; no
AWS account, resource or spend exists yet. Terraform for the env is written later and applied only after this says APPROVED.

1) **What is being deployed.** (a) A dedicated AWS sandbox member account (Organization + IAM Identity Center, GitHub OIDC roles,
   no long-lived keys, budgets and anomaly alerts, a deny-new-spend budget action). (b) Always-on: one Route 53 hosted zone
   `aws.picopaas.xyz` (delegated from DO DNS) and an S3 Terraform state bucket. (c) Per run, created and destroyed by Terraform in
   us-east-1: EKS (standard support) with 2 x t3a.medium nodes in public subnets (no NAT), an NLB, RDS MySQL and RDS PostgreSQL
   (db.t4g.micro, single-AZ, 20 GiB gp3), platform and per-app S3 buckets and IAM users, tagged `project=picopaas, env=p4-test, ttl`.
2) **Why.** P4 needs a real EKS/RDS/S3 environment to prove tenant isolation (IMDSv2, NetworkPolicy), the S3/RDS providers and the
   smoke suite on AWS before any claim that PicoPaaS runs on AWS.
3) **Cost.**
   - **Static: about $0.51/mo** (Route 53 zone $0.50 + state bucket cents). Everything else in (a) is free. This is **not** in the
     $75 baseline; it is in addition. If the account is funded by credits ($100-$200 for new accounts, 6 months) the cash cost is $0.
   - **Per run: about $0.27/hour, $6.40/day running.** A typical 4 h run is about $1.10, a 10 h pentest day about $2.70.
     Spot nodes about $0.22/h; a NAT gateway would add $0.045/h (not planned).
   - **Per app deployed: about $0** (S3 pennies, IAM free), within the RDS/NLB already counted.
   - **Caps proposed:** $5 per run, $25 per month, $8 daily budget alert, nightly teardown of anything past its `ttl`.
   - **Exposure if forgotten:** $6.40/day; alerts at $3/$6 per day and a $5 anomaly threshold.

**Decisions needed from Chas** (also in QUESTIONS.md): whose account/credits, region (proposed us-east-1), the two caps,
spot vs on-demand nodes, and approval to start step 1 of the checklist.

**Rollback.** `terraform destroy` plus the "nothing left billing" checks; the account can be closed (Billing > Close account) and
the NS record for `aws` removed from DO DNS. Prod on DO is untouched throughout.

**Default if unanswered:** nothing is created; P4 stays blocked and P1-P3 continue.
