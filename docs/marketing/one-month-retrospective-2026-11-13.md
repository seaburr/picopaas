---
title: "Twenty-six days, 821 commits: PicoPaaS and Kaisen from empty repo to a platform that builds apps"
slug: one-month-retrospective-picopaas-and-kaisen
tags: picopaas-release, retrospective
meta_description: "A month of PicoPaaS in numbers: 821 commits in 26 days, about 4,000 tests at 96% coverage, 143 security entries tracked, a self-hosted blog, and Kaisen, an AI builder that ships small apps for cents."
---

# Twenty-six days, 821 commits: PicoPaaS and Kaisen from empty repo to a platform that builds apps

The first commit in the PicoPaaS repository is dated 18 October 2026. Today is 13 November. In between there are 821 commits, a platform that hosts real apps with databases, buckets, volumes and backups, a blog that runs on that platform, a security review trail 143 entries long, and an AI builder called Kaisen that writes, tests and deploys small apps onto it for cents.

This is a look back at that month. It is a timeline, with numbers where we have them and a note where we don't.

## Where it started

The idea was small on purpose: a platform for throwaway projects. You give it one container image, add environment variables and secrets, optionally tick a database or a bucket, and the app is live on its own HTTPS address. It is for things you can afford to lose, and the first day's commits say so: a contract document, a cost model, a ledger, CI with secret scanning, Terraform, and a FastAPI scaffold with auth and app CRUD. The repository had 17 commits by the end of day one.

Two constraints shaped everything after. The monthly bill is about $60: one Kubernetes node, one managed MySQL, one load balancer and one Spaces subscription, itemised in the architecture document. And every limit is deliberately small: 0.2 CPU, 384 MB of memory, a 512 MB image, a 50 MB database, a 100 MB bucket. Small caps keep the platform cheap to run and easy to reason about, and they keep us honest about what it is for.

## Week 1 (18 to 24 October): the foundations

The busiest day was 19 October, with 128 commits. By the end of the week the platform had:

- **One image per app**, with a port, a health check and automatic rollback if an update won't start. Public images only, and non-root only.
- **Environment variables and secrets**, encrypted at rest, with references between them so an app can pick up its own database credentials without copy and paste.
- **Databases and storage**: MySQL, and PostgreSQL when an admin attaches a server (the two are equal choices in the code; the hosted instance currently runs MySQL), S3-compatible buckets, and opt-in persistent volumes.
- **Approval-gated sign-ups.** A person asks for access and an admin approves it by hand. That is a deliberate brake, and it is why abuse hardening, egress limits and bandwidth caps arrived in the same week.
- **Backups** of databases, buckets and volumes: daily or weekly, kept off-site, downloadable at any time. The design went through three security reviews before we turned it on, and the findings are in the pentest notes.
- **A CLI** that covers most of what the UI does.

## The blog runs on the platform

The blog you are reading is Ghost, running on PicoPaaS itself, with its database, uploads bucket and backups provisioned the way any user's app would be. That paid for itself early. The first Ghost deployment was slow, and chasing it down produced a devlog post and a better default: Node rendering pages was CPU-bound, and the CPU limit was too tight. WordPress followed, then Directus, Grafana, n8n and the rest.

Today there are 19 documented recipes, each with a tested spec, the env vars it needs and the gotchas we hit. 18 are offered as templates on the landing page: Ghost, WordPress, Directus, Grafana, n8n, Shlink, Miniflux, Forgejo, Memos, Vaultwarden, Wiki.js, Vikunja, Umami, ntfy, IT-Tools, Excalidraw, Gatus and a static site. The recipes also record a rule we keep: when an upstream image misbehaves, we document the fix instead of carrying a patch.

The blog also gave us a running log. Before this post it had 63 published posts, nearly all of them devlogs written as features shipped.

## Security as a habit, not a phase

`docs/PENTEST.md` records 143 numbered entries (K1 to K143). They are a mix of real findings and "checked, and fine" notes. Among the graded ones there is one HIGH (a Kaisen hang watchdog problem, fixed on 2 November), a handful of MEDIUMs and a long tail of LOWs. The MEDIUMs are the kind that make a review worth doing:

- a suspended project that could be revived through another path;
- a partial backup stream that a later restore would have trusted;
- a last-admin race that two concurrent demotions could have won.

All three were fixed, with tests. The most recent pass, on the last three days of work, found no exploitable defect, one LOW that we accepted (a rate-limit check-then-increment race) and a set of notes.

Alongside the reviews there is a standing suite of tenant-isolation tests (network, credentials, pod escape, API authorization) that can run against the live platform, and an edge rule that stops requests for secret-hunting paths such as `.env` files, Git folders and database dumps before they reach an app. The path list is built from the OWASP Core Rule Set plus our own additions and is re-synced weekly. It is a path block, nothing more, and we describe it that way.

We don't publish attack details. We publish that the work happens, how many entries it produced, and that each finding has a fix or a recorded reason.

## The UI redesign

On 6 and 7 November the interface was rebuilt around a left-hand project tree and flat, table-first pages, with text actions instead of decorative icons. Two feedback rounds followed within days: readability, projects in the URL, settings pages that fit, a softer dark mode and a light mode. Later passes added a reworked apps table, an announcement banner, a health-advice section on the app page that points at what to check when an app is unhealthy, and a rejected-requests view for admins.

Every page follows one width rule that a CI check enforces, and a Playwright suite drives the real app in a browser.

## Kaisen

Kaisen began as a separate project: an agent kit and a standalone service that built apps from a description. By 29 October a Preview version was running inside PicoPaaS, and the standalone service was retired the next day. Kaisen is now a feature of the platform, not a neighbour.

What it is: you describe a small app in plain words, and Kaisen writes it, writes tests, runs them, has a separate reviewer pass look at the result, and deploys it as an ordinary PicoPaaS app, with the same address, database and backups as any other. You steer by asking for changes in the same conversation.

What it is not: free, unmetered or unattended. It runs on the project's own Anthropic API key, and the controls are yours:

- **Caps** on spend per run, per day and per build, plus per-member daily limits. A cap is checked before the model call, not after.
- **Restore points**, so a change that goes sideways can be rolled back, and **Fork**, which copies a build's files into a new build without copying secrets or uploads.
- **Stop**, which ends a run that is heading the wrong way.
- **Uploads**, so you can hand it a file as part of the conversation.
- **A package check** at the end of each run that pushes the build onto current stable dependency versions.
- **Web access off** until you turn it on.
- **Your code**: download it, or have each build pushed to GitHub.

The harness improved the way the platform did, by measuring. We built an evaluation script that runs fixed briefs through the public API on a metered key and records whether the app deployed, how many endpoint checks passed, how many turns it took and what it cost. Three rounds of that, with findings, fixes and the fixes landing, are in earlier posts on this blog. Evals are manual and never run in CI because they cost real money; a full run on the cheapest model costs about $0.20 to $0.30.

The documented costs of real builds:

- A DAW-style music app: $0.21 across 4 runs on Haiku, never hitting the 40-turn limit.
- Fancy Docs: about $0.05.
- This week's test builds on the live platform: a tip calculator for about $0.012, a notes API with one change request for about $0.009, and a markdown previewer on Sonnet for about $0.07.

Kaisen is best for small web apps. It doesn't do email, background workers or scheduled jobs yet, and it is still labelled Preview.

## What "mature" means here

We don't mean big. We mean you can check the claims.

| Measure | Value | Source |
|---|---|---|
| Time since first commit | 26 days (18 Oct to 13 Nov) | git history |
| Commits | 821 | git history |
| Backend tests in CI | 4,048 passed, 97 skipped | latest CI run on main |
| Coverage | 96% of 17,527 statements; CI floor 93% | same run, `.coveragerc` |
| Live-server CI jobs | MySQL, PostgreSQL 16, a real S3-compatible store | CI workflow |
| Security entries | 143 (K1 to K143) | `docs/PENTEST.md` |
| Recipes and templates | 19 documented, 18 on the landing page | `docs/RECIPES.md`, landing page |
| Infrastructure | about $60 per month | `docs/ARCHITECTURE.md` |
| Posts on this blog | 63 before this one | Ghost admin |

Cost discipline is part of maturity too. The platform has a written cost model, and nothing paid gets provisioned without an approved request. We tore down a managed PostgreSQL server on 10 November to save its roughly $15 a month. Full test runs are limited on purpose: CI is the one full run, and agents run focused tests while they work. Kaisen evals are rare for the same reason.

## Where it is going

The direction is bring your own cloud: PicoPaaS built to run wherever you deploy it. There is a generic target today that runs on any Kubernetes with Garage for object storage and in-cluster databases, and a written plan for AWS and GCP. We aren't putting dates on the plan.

## Try it

PicoPaaS is a dev and test platform with no uptime guarantee, and please don't store personal data on it. Access is approved by hand, so there is a short wait: request access on the sign-up page and an admin will be in touch if it is approved. Bring a public, non-root container image, start from a template, or describe an app to Kaisen with your own Anthropic key.

---

## Summary for link cards and social

PicoPaaS went from an empty repository to a working platform in 26 days: 821 commits, about 4,000 passing tests at 96% coverage, 143 security entries tracked, and a blog hosted on the platform itself. Kaisen, its AI builder, writes, tests and deploys small apps onto it, and the builds we documented cost from about one cent to 21 cents. It is a dev and test platform with approval-gated access and no uptime guarantee. This is the month in numbers.

## Meta description

A month of PicoPaaS in numbers: 821 commits in 26 days, about 4,000 tests at 96% coverage, 143 security entries tracked, a self-hosted blog, and Kaisen, an AI builder that ships small apps for cents.

## Proposed landing/README changes

Applied 2026-11-13 after Chas's "Looks good" (README cost, landing Kaisen card cost line, footer link).

- README intro says "It runs on DigitalOcean Kubernetes for about $75/mo." ARCHITECTURE and CLAUDE.md say about $60 (managed PostgreSQL was torn down on 2026-11-10). Proposed: "for about $60/mo."
- Landing "Describe an app" card could carry one concrete number: "A small app can cost a few cents: our test builds ran from about $0.01 to $0.07." Source: ledger, 2026-11-13 live validation.
- Add a link to this retrospective next to the Blog link once it is published, for example "A month of PicoPaaS and Kaisen, in numbers".
