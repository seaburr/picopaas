# Questions & to-dos for Chas

**This file is yours.** Write answers inline, any style; one word is fine. Tick boxes when done.
Claude reads it at the start of every session, carries answers into `docs/ARCHITECTURE.md` /
`docs/requests/`, and moves answered items to **Resolved** at the bottom. The request files have the
background on each item if you want it.

## Open questions

- (none right now)

## To-dos (only you can do these)

- (none right now)

## Resolved
- 2026-10-19: DOKS "insufficient capacity" explained: a region's API size list shows what has capacity right now. NYC3 lists
  only -amd/-intel variants of the small sizes (no plain s-1vcpu-2gb / s-2vcpu-4gb); ATL1, MKC1 and MEM1 list no small
  sizes at all; RIC1 lists only -intel. Node → `s-1vcpu-2gb-amd` in NYC3 (~$14). The terraform workflow now fails early if the
  size isn't listed. No DO ticket needed.
- 2026-10-19: NYC3 also refused s-2vcpu-4gb → Chas: smallest SKUs everywhere (node `s-1vcpu-2gb` $12, MySQL `db-s-1vcpu-1gb` $15.15); ≈ $44.15/mo. Scale later.
- 2026-10-19: ATL1 refused DOKS at every size (incl. 2 GB) → compute + MySQL move to **NYC3**, back to `s-2vcpu-4gb` ($24); total ≈ $60.30/mo. Spaces buckets stay in ATL1.
- 2026-10-19: ATL1 also out of -amd and -intel 4 GB nodes → Chas chose `s-2vcpu-2gb` ($18) in ATL1; total ≈ $54.30/mo.
- 2026-10-19: ATL1 out of s-2vcpu-4gb capacity → node `s-2vcpu-4gb-amd` (~$28, total ≈ $64.30/mo). MySQL version 8.4 (only one DO offers).
- 2026-10-19: Credentials come from GitHub secrets, not TFC vars: the workflow sets the TFC workspace to local execution (TFC = state only).
- 2026-10-19: Load balancer: yes. Final variant managed MySQL (AMD) + LB ≈ $60.30/mo. History rewrite run by Chas (force-pushed).
- 2026-10-19: Managed MySQL, Premium AMD 1 GiB in ATL1, $19.30/mo (`db-amd-1vcpu-1gb`).
- 2026-10-19: Autoscaler `max_nodes = 2` approved.
- 2026-10-19: Let's Encrypt email → `yz80nomo+lepaas@gmail.com`.
- 2026-10-19: Fake test password in history: rewrite history (force-push OK) instead of `.gitleaksignore`, and add
  guards: `.githooks/pre-commit` gitleaks hook, CI fails if `.gitleaksignore` exists, no-allowlist rule in CLAUDE.md.
- 2026-10-19: All to-dos done (DNS, DO token, Spaces key, TFC workspace, GHCR already public). Chas's hand-made
  bucket `pico-paas-s3-a` (atl1) is adopted as the platform bucket via a Terraform `import` block.
- 2026-10-18: Region ATL1 (NYC3 fallback).
- 2026-10-18: No root images.
- 2026-10-18: Request 0002: credential theft out of scope (option A); DB server + Spaces kept off the public
  internet where possible; k8s API may stay public.
- 2026-10-19: Apps + platform on `raisehellpraisedale.xyz`; seaburr.io no longer used by the platform.
- 2026-10-19: Internal-team audience; arbitrary images OK with: signups inert until approved, admin
  suspend/delete for users and apps, audit log, SMTP egress blocked. `hostUsers: false` not adopted.
- 2026-10-19: Terraform state in Terraform Cloud (org `seaburr`), like other projects.
