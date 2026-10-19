# PicoPaaS

A tiny Heroku-style PaaS for quick throwaway projects. You deploy one Docker image, wire up env vars and
secrets, and optionally get a MySQL database (100 MB) and S3-compatible storage (250 MB). It runs on
DigitalOcean Kubernetes for under $40/mo.

Status: early design. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [docs/LEDGER.md](docs/LEDGER.md).

Docs: [User guide](docs/USER_GUIDE.md) | [API](docs/API.md) | [Operations](docs/OPERATIONS.md) | [CLI](docs/CLI.md)
