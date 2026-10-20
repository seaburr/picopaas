# Questions & to-dos for Chas

**This file is yours.** Write answers inline, any style; one word is fine. Tick boxes when done.
Claude reads it at the start of every session, carries answers into `docs/ARCHITECTURE.md` /
`docs/requests/`, and moves answered items to **Resolved** at the bottom. The request files have the
background on each item if you want it.

## Open questions

- **Docs review (2026-10-20), decisions for you.**
  1. GitHub issue #9 ("status_detail 'deployment failed' is too vague") looks fixed: `STATUS_MESSAGES` in `app/reconciler.py`
     now give a reason and a next step for every failure. Close it? (I couldn't confirm it against live behaviour.)
  2. The landing, privacy and dev-banner text said "Sign-up is open". I reworded it to "An administrator approves every
     account by hand" so it matches the **Request access** flow. The privacy page date is now 2026-10-20. Confirm the wording
     is what you want to publish.
  3. The user docs and UI say "administrator". The API still returns the role value `uberadmin` and `docs/API.md` documents it.
     Keep the role value as is, or rename it (a breaking API change)?
  4. Not verified against a live run: the Python 3.13 `VERIFY_X509_STRICT` workaround in the User Guide for DigitalOcean's
     CA, and the claim that credential rotation repairs an app whose stored secrets are unreadable. Please spot-check once.

## To-dos (only you can do these)

- [ ] **Update CLAUDE.md's "Budget: under $40/mo" hard rule.** It predates the approved ~$60/mo setup (about $88 with the
      second node). Update it if you agree. Background: `docs/ARCHITECTURE.md` §1, `docs/requests/0001-core-infra.md`.

- [ ] **Add GitHub secrets `E2E_USER_TOKEN` and `E2E_ADMIN_TOKEN` so the weekly quota test can run.**

      *What it is.* `.github/workflows/quota-e2e.yml` runs every Monday (and on demand from the Actions tab). It logs in to the
      live platform as a test customer, deploys throwaway "probe" apps with a database and a bucket, fills them past the
      50 MB / 100 MB caps, and checks that: writes get blocked, reads keep working, one long-running connection can't blow far
      past the cap, and the user can recover (empty the table / "Free up space") and get write access back. Then it deletes the
      apps. It takes up to about an hour (the DB check runs every minute, the bucket check every 5).

      *Why.* On 2026-10-19 a live run found a DB could reach 6x its cap and a full bucket could never recover; both were fixed.
      Nothing else would notice if a later change broke enforcement again. This turns "the caps work" into something
      re-proven every week.

      *What the two secrets are.* Both are PicoPaaS API keys (`pp_sk_…`, made on the **API keys** page), not GitHub tokens:
      - `E2E_USER_TOKEN`: a key for an ordinary approved, non-admin test account (today: `jimmy@gmail.com`). The test acts as
        this customer.
      - `E2E_ADMIN_TOKEN`: a key for your admin account. The test uses it to raise the test user's app limit during the run,
        restore it afterwards, and delete the test apps even if something fails halfway.

      *Security note.* The admin key can do anything an admin can, and any workflow on `main` could read it. Make a
      **dedicated** key for this (name it `ci-quota-e2e`), not one you use yourself, so you can revoke it on its own. If you'd
      rather not keep an admin key in GitHub at all, say so and Claude can narrow what the test needs.

      *How (about 5 minutes).*
      1. Log in as the test user, open **API keys**, click **Create key**, name it `ci-quota-e2e`, and copy it.
      2. Log in as yourself (admin) and do the same.
      3. In GitHub, go to repo **Settings → Secrets and variables → Actions → New repository secret** and add `E2E_USER_TOKEN`
         and `E2E_ADMIN_TOKEN` with those values. Or from a terminal: `gh secret set E2E_USER_TOKEN` (it prompts; paste the key).
      4. Optional: **Actions → quota-e2e → Run workflow** to see it pass once.

      Until both exist the workflow skips itself with a notice; nothing breaks. Details: `tests/e2e/README.md`.
      If it says `failed` > 0 or `listing failed`, the DB admin (`doadmin`) cannot kill other users' sessions; the platform then rolls
      the app's pod as a fallback (see the 2026-10-19 ledger entry), but tell Claude so it can look for a better mechanism.

## Resolved
- 2026-10-20: Worker node `s-2vcpu-4gb-amd` costs $28/mo (Chas, from the DO bill).
- 2026-10-20: bootstrap.md caps updated by Chas (DB 50MB, bucket 100MB, CPU 100m, RAM 384MB, image 256MB, scratch disk 250MB);
  matches `app/config.py`.
- 2026-10-19: **Own the platform encryption keys.** FERNET_KEY decrypts every secret the platform stores in MySQL; losing it
      loses that data (it happened in the 2026-10-19 cluster rebuild, harmless then). Claude put generated keys in
      repo secrets `PICOPAAS_SECRET_KEY` / `PICOPAAS_FERNET_KEY`, but GitHub secrets are write-only, so nobody has a
      copy. While nothing valuable is encrypted yet: generate your own, save them in your password manager, and
      overwrite both secrets (`python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"`
      for FERNET_KEY; any 48+ random chars for SECRET_KEY). Then tell Claude to re-run bootstrap. NOTE: Both updated with the same value however if you do not need both, please delete the unused one so there is less confusion about the purpose of the key and what needs to be generated when deploying.
  **Done:** single key now (FERNET_KEY; SECRET_KEY derived, old key kept for rotation); `PICOPAAS_SECRET_KEY` can be deleted after the next bootstrap succeeds.
- 2026-10-19: DOKS "insufficient capacity" explained: a region's API size list shows what has capacity right now. NYC3 lists
  only -amd/-intel variants of the small sizes (no plain s-1vcpu-2gb / s-2vcpu-4gb); ATL1, MKC1 and MEM1 list no small
  sizes at all; RIC1 lists only -intel. Node → `s-1vcpu-2gb-amd` in NYC3 (~$14). The terraform workflow now fails early if the
  size isn't listed. No DO ticket needed.
- 2026-10-19: NYC3 also refused s-2vcpu-4gb → Chas: smallest SKUs everywhere (node `s-1vcpu-2gb` $12, MySQL `db-s-1vcpu-1gb` $15.15); ≈ $44.15/mo. Scale later.
- 2026-10-19: ATL1 refused DOKS at every size (incl. 2 GB) → compute + MySQL move to **NYC3**, back to `s-2vcpu-4gb` (later confirmed as `s-2vcpu-4gb-amd` at $28; see ARCHITECTURE §1); total ≈ $60.30/mo at the time. Spaces buckets stay in ATL1.
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
- [x] 2026-10-20: doadmin can't kill app sessions (MySQL 1095 'not owner of thread'); the pod-roll fallback cut a 300MB fill at ~88MB. Now the platform kills sessions as the app's own user first.
