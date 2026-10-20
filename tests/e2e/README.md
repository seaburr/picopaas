# Live end-to-end + pentest harness

Runs against a deployed PicoPaaS. Everything is skipped unless `E2E_USER_TOKEN` and `E2E_ADMIN_TOKEN` are set,
so `python -m pytest tests/ -q` stays offline-safe.

```bash
source .venv/bin/activate
PICOPAAS_URL=https://platform.raisehellpraisedale.xyz \
E2E_USER_TOKEN=pp_sk_... E2E_ADMIN_TOKEN=pp_sk_... \
python -m pytest tests/e2e tests/pentest -v
```

| Env | Default | Meaning |
|---|---|---|
| `PICOPAAS_URL` | https://platform.raisehellpraisedale.xyz | API base |
| `APP_DOMAIN` | raisehellpraisedale.xyz | domain for `<hostname>.<APP_DOMAIN>` app URLs |
| `E2E_USER_TOKEN` | required | `pp_sk_` key of a test customer (approved, non-admin) |
| `E2E_ADMIN_TOKEN` | required | `pp_sk_` key of the uberadmin |
| `E2E_IMAGE_TAG` | latest | tag of `ghcr.io/seaburr/picopaas-testapp-{hello,probe,toolbox}` |
| `E2E_SLOW` | unset | `1` enables the quota-fill tests (DB polled every 60s, storage every 5 min; up to ~20 min each). Run by `.github/workflows/quota-e2e.yml` weekly |
| `E2E_APP_PORT` | 8080 | port the test apps listen on |
| `E2E_BIG_IMAGE` | unset | public non-root image over 256MB, enables the size-cap test |

Side effects: apps are named `e2e-<random>` and removed (admin delete) even on failure. The customer's
`app_limit` is raised to at least 6 for the run and restored. A second user `e2e-<random>@picopaas-e2e.dev`
is registered, approved by the admin, and deleted at the end. Run only against a platform you own.
Pentest details: `tests/pentest/README.md`.
