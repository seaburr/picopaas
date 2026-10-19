# Questions & to-dos for Chas

**This file is yours.** Write answers inline, any style; one word is fine. Tick boxes when done.
Claude reads it at the start of every session, carries answers into `docs/ARCHITECTURE.md` /
`docs/requests/`, and moves answered items to **Resolved** at the bottom. The request files have the
background on each item if you want it.

## Open questions

- (none right now)

## To-dos (only you can do these)

- [ ] **Terraform Cloud vars aren't reaching the plan** (run 39270616109: DO API 401 + "Spaces credentials not
      configured"; the same token works from GitHub Actions). In TFC → `picopaas` → Variables, check that these three
      exist with exactly these names, category **Environment variable** (not "Terraform variable"), marked sensitive:
      `DIGITALOCEAN_TOKEN`, `SPACES_ACCESS_KEY_ID`, `SPACES_SECRET_ACCESS_KEY`. If they're in a variable set, make
      sure it's applied to this workspace. Tell Claude when done and it re-runs the plan.

## Resolved
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
