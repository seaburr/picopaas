# PicoPaaS: Claude Code Context

A tiny Heroku-like PaaS for throwaway projects: one Docker image per app, env vars/secrets, and optional
MySQL DB (100MB) and Spaces bucket (250MB). Caps: 250m CPU, 512MB RAM, 512MB image. Runs on DigitalOcean (DOKS + Spaces).

**`bootstrap.md` is the non-negotiable contract. Read it before doing anything.** Then read:
- `docs/LEDGER.md`: what's done and what's next (start at "Now / Next")
- `docs/ARCHITECTURE.md`: design decisions and the cost model
- `docs/requests/`: infra requests to Chas (what / why / monthly cost). Never provision without an approved request.

## Hard rules
- Budget: under $40/mo total. Any new paid resource needs a request in `docs/requests/`.
- Tokens are scarce (Claude Pro). Work in small chunks, add a ledger line at the START and END of each chunk,
  and prefer `sonnet` subagents for implementation.
- **Only the `scm-security` agent commits and pushes.** Other agents leave changes in the working tree and
  record them in the ledger.
- Never commit secrets: no `.env`, kubeconfig, `*.tfvars`, `*.tfstate`, or tokens. Secrets live in GitHub repo
  secrets (requested from Chas) or k8s Secrets.

## Layout (target, modeled on github.com/seaburr/tribal)
```
app/            FastAPI backend (routers/, models.py, schemas.py, reconciler/, providers: k8s, mysql, spaces)
frontend/       Vue 3 + Vite + Pinia + Tailwind (built into the app image → static/)
tests/          pytest, SQLite in-memory, real login flow (no auth mocks); infra clients faked via interfaces
alembic/        migrations
infra/          Terraform (DO), state in Spaces
deploy/         k8s manifests/Helm values (ingress-nginx, cert-manager, mysql, platform)
docs/           ledger, architecture, requests
```

## Commands (fill in as they come to exist)
```bash
python -m pytest tests/ -v
cd frontend && npm run dev | npm run typecheck
```
