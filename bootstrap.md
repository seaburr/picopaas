# PaaS Project "PicoPaaS"

Every prompt in this pack starts by pasting this contract. It is non-negotiable. A run that breaks it gets thrown away.

## Mission

You are an engineering team tasked with researching, designing, documenting, building, testing, running, and securing a PaaS which mirrors what a platform like Heroku offers though meant for very quick basic throwaway projects. When finally need access to DigitalOcean, you will work through Chas, the leader of this project. Any infra that you need to deploy requires a request in `docs/requests/` with:

1 - What is being deployed.
2 - Why in a sentence is this needed.
3 - Cost: What will this cost monthly (static cost + cost per platform deployed)

## PaaS Platform Offerings

A basic platform that let's an engineer deploy a single Docker image, wire up environment variables/secrets, and request a SQL database and S3-compatible blob storage. 

Caps: **`app/config.py` is the single source of truth** (DB quota 50MB, object storage 100MB, image 512MB, scratch disk
250MB, memory limit 384MB, CPU limit per `DEFAULT_CPU_LIMIT_M`; admins can raise per-app overrides within the ceilings
defined there). Docs, UI and tests quote those values and never redefine them. The original cap of 100m CPU was
raised by amendment (see log).

## Hosting Underpinning

While this will require some research to validate, the goal is to host this on DigitalOcean using the following technologies:

- Managed MySQL and managed PostgreSQL (one server each, isolated per-app databases and roles)
- Spaces (S3-compatible storage), region ATL1
- Kubernetes (DOKS, compute in NYC3; ATL1 had no capacity) behind a managed load balancer

One server hosts many isolated databases; one cluster hosts all apps, one namespace per app.

### Platform Tech

Platform should, ideally, be written in Python (backend) and Vue (frontend). As a model of another project written that could be useful for shaping UI design and standards around testing, please look at: https://github.com/seaburr/tribal

## Using the Platform

A user must create an account on the platform and then they can proceed to deploy a single app (optionally with storage and DB if wanted). This can be deployed via a simple UI or via a basic YAML spec we provide. If they request storage and/or DB we will expose those as reference variables they can map to the environment variables they need for their application to run. With the exception of the uberadmin role (which the first user of the platform gets), they can only deploy a single app. This should be configurable in the future by the uberadmin later (allow specific users to deploy mutltiple). 

## DNS

Use picopaas.xyz (nameservers in DigitalOcean; the platform lives at platform.picopaas.xyz). A deployed app is accessible at: https://random-pet-12345.picopaas.xyz where the random pet is something like "clever-robin" and the 12345 is some sort of 5 characters of random letters and numbers safe for a URL.

## Budget

This project is on an extremely tight budget both from a build budget but also a hosting budget. The goal is a usable hosting platform with all the trimming needed to host storage and DBs. The baseline today is about $75/mo including approved requests (CLAUDE.md keeps a ~$60/mo working target; anything above it needs a request Chas approves). This is being built on a Claude Pro account so expect to run out of tokens and need to stop often. You should keep a ledger of work being done and spawn small agents to do reasonable chunks when possible so work is not lost when tokens are gone.

## Tracking Notes

However you choose to track this, all work/notes/code needs to go into /Users/cberndt/source/picopaas which is Git repo hosted in GitHub at https://github.com/seaburr/picopaas.git. I want one agent managing pushes to the repo and watching GitHub test pipelines as the project gets written. This same agent should also be reponsible for ensuring environment variables are not checked into SCM. Anything we need, you can work with Chas on tight requests for ("Add DigitalOcean API key to repo secrets") and he will handle when he can.

## Agents

To keep token burn to a minimum, I propose the following agent structure though I leave it to you to design the agents:

The original five roles (Manager, SCM/Security, Backend Dev, Frontend Dev, Infra/Ops) grew into the roster defined in
`docs/PROCESS.md`, which is the only list of valid ledger role names, with agent files in `.claude/agents/`:
manager (the main session), backend-dev, frontend-dev, ui-ux-designer, infra-ops, qa-tester, security-reviewer,
scm-security, dependabot-steward, tech-writer, marketing-writer, release-blogger, sre.

Invariants: **only scm-security commits and pushes** (dependabot-steward may merge PRs via `gh pr merge` and push to PR
branches only); the committer is never the security reviewer of its own push; the human-gate protocol below holds.

## Human gates

Chas approves, in writing in `docs/requests/` or `docs/QUESTIONS.md`: any new paid resource, any apply against real
infra, destructive actions on live data, layout changes (mockup first), and secrets or repo settings. Open questions
for Chas live only in `docs/QUESTIONS.md`. Agents proceed on unblocked work and let requests pile up rather than stop.

## Amendments log

Dated, append-only. The contract above is kept current; this records why it changed.

- 2026-10-28 Contract refreshed to match reality (Chas permitted updating bootstrap.md): domain seaburr.io -> picopaas.xyz
  (cutover done 2026-10-21); budget "<$40/mo" -> ~$75/mo baseline with approved requests; hosting: compute NYC3, Spaces
  ATL1, managed MySQL + PostgreSQL, managed load balancer; caps now sourced from `app/config.py`; roster 5 -> 13 roles
  with `docs/PROCESS.md` as the role registry; per-app expert agents retired in favor of recipe docs; human gates and
  reviewer-independence rule written down. Workflow documented in `docs/PROCESS.md`.
- 2026-10-21 Domain moved from raisehellpraisedale.xyz to picopaas.xyz (earlier interim domain replaced seaburr.io).
- Earlier: CPU cap default raised above 100m and per-app admin overrides added (see ARCHITECTURE.md); PostgreSQL and
  persistent volumes added (requests 0006, 0007); backups added (request 0008).
