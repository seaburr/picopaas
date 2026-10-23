# PicoPaaS: Claude Code Context

A tiny Heroku-like PaaS for throwaway projects: one Docker image per app, env vars/secrets, and optional
MySQL DB (50MB) and Spaces bucket (100MB). Caps: 100m CPU, 384MB RAM, 512MB image (single source: app/config.py). Runs on DigitalOcean (DOKS + Spaces).

**`bootstrap.md` is the non-negotiable contract. Read it before doing anything.** Then read:
- `docs/LEDGER.md`: what's done and what's next (start at "Now / Next")
- `docs/ARCHITECTURE.md`: design decisions and the cost model
- `docs/QUESTIONS.md`: **Chas's file.** Open questions + to-dos for him; he answers inline. Read it first every session,
  carry answers into ARCHITECTURE/requests, and move answered items to its Resolved section. Put every new question for
  Chas here (one place to look), linking to the request file for background.
- `docs/requests/`: infra requests to Chas (what / why / monthly cost). Never provision without an approved request.

## Hard rules
- Budget: approx. $60/mo total. Any new paid resource needs a request in `docs/requests/`.
- Tokens are scarce (Claude Pro). Work in small chunks, add a ledger line at the START and END of each chunk,
  and prefer `sonnet` subagents for implementation.
- **Only the `scm-security` agent commits and pushes.** Other agents leave changes in the working tree and
  record them in the ledger.
- Secret scanning: every clone runs `git config core.hooksPath .githooks` (gitleaks on staged changes). **Never**
  add `.gitleaksignore`, inline `gitleaks:allow`, or allowlists; CI fails if `.gitleaksignore` exists. Test fixtures use
  values that don't look like secrets (e.g. `not-the-password`), so a finding means change the value.
- Never commit secrets: no `.env`, kubeconfig, `*.tfvars`, `*.tfstate`, or tokens. Secrets live in GitHub repo
  secrets (requested from Chas) or k8s Secrets.

## Layout (target, modeled on github.com/seaburr/tribal)
```
app/            FastAPI backend (routers/, models.py, schemas.py, reconciler/, providers: k8s, mysql, spaces)
frontend/       Vue 3 + Vite + Pinia + Tailwind (built into the app image → static/)
tests/          pytest, SQLite in-memory, real login flow (no auth mocks); infra clients faked via interfaces
alembic/        migrations
infra/          Terraform (DO), state in Spaces
deploy/         k8s manifests/Helm values (traefik, cert-manager, mysql, platform)
docs/           ledger, architecture, requests
```

## Commands
```bash
# one-time setup
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# run the API (SQLite at ./data/picopaas.db by default; set DATABASE_URL for MySQL)
source .venv/bin/activate
uvicorn app.main:app --reload

# tests (SQLite in-memory, real register/login flow, no auth mocks)
source .venv/bin/activate
python -m pytest tests/ -v

# migrations (DATABASE_URL / SECRET_KEY / FERNET_KEY come from the environment, see app/config.py)
source .venv/bin/activate
alembic upgrade head
alembic revision --autogenerate -m "describe the change"

# frontend (once frontend/ exists)
cd frontend && npm run dev | npm run typecheck
```

Required env vars outside `ENVIRONMENT=dev`: `SECRET_KEY` (JWT signing), `FERNET_KEY` (encrypts env var
values at rest — generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`).
Dev/test runs use fixed insecure defaults baked into `app/config.py`.

Reconciler/scheduler env vars (all optional; see `app/scheduler.py`, `app/config.py`):
- `RECONCILER_ENABLED=1`: start the APScheduler jobs (reconcile every 15s, quota poll every 5 min) in the app lifespan.
  Off by default and never set in tests. With MySQL, a `GET_LOCK('picopaas-reconciler')` keeps replicas from double-running.
- `MYSQL_EGRESS_MODE` = `in-cluster` (ns `picopaas-data`, pod `app=mysql`) | `cidr` (default), plus `MYSQL_EGRESS_CIDR` for a managed DB.
- `BOOTSTRAP_DB_HOST` / `_PORT` / `_USER` / `_PASSWORD`: if all set (port defaults 3306) and no DbServer exists, insert one named `primary`.
