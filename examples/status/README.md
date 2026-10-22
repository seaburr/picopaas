# Status / uptime page on PicoPaaS

A self-hosted status page ([Gatus](https://github.com/TwiN/gatus), pinned `v5.37.0`) running as an ordinary
PicoPaaS app. It probes public endpoints on a schedule and shows a live dashboard. The shipped config is a
generic template that renders green out of the box (example.com + a DNS check).

## Make it yours
1. Fork/copy `examples/status/` and edit `config.yaml`: replace the example endpoints with your own public URLs
   and conditions (see the [Gatus docs](https://github.com/TwiN/gatus#configuration)), and set `ui.title`.
2. For alerting, uncomment the `alerting:` block and add `alerts:` to endpoints. Keep secrets out of the file:
   reference them as `${ENV_VAR}` and set the value as an app env var/secret in PicoPaaS.
   Outbound SMTP is blocked, so use a webhook provider (Discord, Slack, ntfy, Telegram), not email.
3. Storage is in-memory (no persistent volume), so check history resets when the pod restarts.

## Deploy on PicoPaaS
1. Build and push: changes under `examples/status/**` trigger the `examples` workflow, which publishes
   `ghcr.io/seaburr/picopaas-example-status:<sha>` (for your fork, point your own CI at your own registry
   and make the package **public**; PicoPaaS pulls anonymously).
2. Create an app with image `ghcr.io/seaburr/picopaas-example-status:latest` (or your own image), port
   **8080**, no database or bucket needed, plus any env vars your config references.
3. Optional: ask an admin for a reserved subdomain (e.g. `status`) for a nicer URL.

## Limitation
It can monitor **any public endpoint** (tenant egress allows the public internet), but it cannot probe
PicoPaaS's own public URLs from inside the cluster: traffic to the platform's load balancer from within it
fails (LB hairpin). That is tenant isolation working as designed. Use an external monitor for the platform itself.
