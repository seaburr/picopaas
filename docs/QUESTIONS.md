# Questions & to-dos for Chas

**This file is yours.** Write answers inline, any style; one word is fine. Tick boxes when done.
Claude reads it at the start of every session, carries answers into `docs/ARCHITECTURE.md` /
`docs/requests/`, and moves answered items to **Resolved** at the bottom. The request files have the
background on each item if you want it.

## Open questions

1. **Hosting variant** (request 0001). With an LB: managed MySQL + LB ≈ $61/mo. Without one: managed MySQL ≈ $49,
   in-cluster MySQL ≈ $30. Middle option: in-cluster MySQL + LB ≈ $42. Claude leans **managed MySQL**: it's simpler
   and safer, with no root/initdb handling. ATL1 managed MySQL is "Premium Intel" only and its exact price is
   unconfirmed, so please check it in the DO panel.
   **Answer:**

2. **Autoscaler ceiling:** OK to let the cluster grow to `max_nodes = 2`? (Worst case +$24/mo while the
   second node is up.)
   **Answer:**

3. **Let's Encrypt contact email:** currently `infra@seaburr.io` (only receives cert-expiry warnings).
   It'll be in the public repo. Keep it or use another address?
   **Answer:**

4. **`.gitleaksignore`:** one-line exception for a fake test password in old commit `58e2897`
   (history can't be cleaned without a force-push). Keep it?
   **Answer:**

## To-dos (only you can do these)

- [ ] Move `raisehellpraisedale.xyz` nameservers to `ns1/ns2/ns3.digitalocean.com` at your registrar, then add
      the domain in the DO control panel (request 0003)
- [ ] DO API token (read/write) → GitHub repo secret `DIGITALOCEAN_TOKEN`
- [ ] Spaces access key → GitHub repo secrets `SPACES_ACCESS_KEY_ID` / `SPACES_SECRET_ACCESS_KEY`
- [ ] Terraform Cloud: create workspace `picopaas` in org `seaburr`, add the three values above as **sensitive
      env vars** there, and add a TFC API token as GitHub secret `TF_API_TOKEN`
- [ ] Make the GHCR package `ghcr.io/seaburr/picopaas` public (repo → Packages → package settings → visibility)

## Resolved
- 2026-10-18: Region ATL1 (NYC3 fallback).
- 2026-10-18: No root images.
- 2026-10-18: Request 0002: credential theft out of scope (option A); DB server + Spaces kept off the public
  internet where possible; k8s API may stay public.
- 2026-10-19: Apps + platform on `raisehellpraisedale.xyz`; seaburr.io no longer used by the platform.
- 2026-10-19: Internal-team audience; arbitrary images OK with: signups inert until approved, admin
  suspend/delete for users and apps, audit log, SMTP egress blocked. `hostUsers: false` not adopted.
- 2026-10-19: Terraform state in Terraform Cloud (org `seaburr`), like other projects.
