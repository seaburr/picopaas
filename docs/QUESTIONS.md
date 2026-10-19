# Questions & to-dos for Chas

**This file is yours.** Write answers inline, any style; one word is fine. Tick boxes when done.
Claude reads it at the start of every session, carries answers into `docs/ARCHITECTURE.md` /
`docs/requests/`, and moves answered items to **Resolved** at the bottom. The request files have the
background on each item if you want it.

## Open questions

1. **Load balancer + go-ahead to apply.** MySQL is settled (Premium AMD). Remaining choice:
   with LB ≈ **$60.30/mo** (robust when the autoscaler adds/replaces nodes) or without ≈ **$48.30/mo**
   (ingress on node ports + DNS-sync CronJob). Claude recommends the LB. Reply "LB, apply" (or "no LB, apply")
   and Claude runs the Terraform Cloud plan, then apply.
   **Answer:**

2. **Run the history-rewrite script** (your answer to old Q4). Claude's permissions block rewriting `main`, so
   it's ready for you: in Claude Code type
   `! bash /private/tmp/claude-501/-Users-cberndt-source-picopaas/bab185d4-d6b4-41d5-88b6-03cd0bd97143/scratchpad/rewrite-history.sh`
   It backs up the repo, scrubs the fake password from all commits, deletes `.gitleaksignore`, re-scans, and force-pushes.
   **Done?**

## To-dos (only you can do these)

- (none right now)

## Resolved
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
