# PicoPaaS CLI

`cli/picopaas.py` (needs `httpx`). Create a `pp_sk_` API key in the UI, then:

```bash
export PICOPAAS_TOKEN=pp_sk_...            # PICOPAAS_URL defaults to https://picopaas.seaburr.io
python cli/picopaas.py apply -f app.yaml --secret API_TOKEN=s3cret
python cli/picopaas.py apps
python cli/picopaas.py status my-app
python cli/picopaas.py rotate my-app --db      # or --storage
```

Example `app.yaml` (max 64KB, no YAML anchors/aliases, unknown keys rejected):

```yaml
apiVersion: picopaas/v1
name: my-app
image: ghcr.io/me/app:1.2
port: 8080
resources: { db: true, storage: false }
env:
  LOG_LEVEL: info
  DATABASE_URL: ${{ db.url }}
secrets:
  - API_TOKEN
```

`apply` creates the app, or updates it if you already have one with that `name`. On update, a secret
without `--secret` keeps its stored value. Env values must be strings (quote numbers and booleans).
