# Recipes

PicoPaaS hosts real apps, not just toy images. This page walks through Ghost (a popular CMS) end to end, then
lists the lessons that apply to any image you bring.

## Deploying Ghost

The files are in `examples/ghost/` in the repo (`Dockerfile`, `docker-entrypoint.sh`).

### Why a custom image

The official `ghost:5-alpine` image starts as root and `su-exec`s down to `node`. PicoPaaS rejects root and forces
a non-root numeric UID (10001). So we build a thin image on top that runs as 10001 directly.

### The image

`FROM ghost:5-alpine`, plus:

- An `/etc/passwd` entry for UID 10001. Node's `os.userInfo()` throws for a UID with no entry.
- `content/` seeded from `content.orig` at build. The stock root entrypoint normally does this and we bypass it.
- `chown` of `content/` only. Chowning the whole tree duplicates it in a new layer and doubles the image.
- The S3 storage adapter, behind build-arg `WITH_S3=1` (the published image is built with it).
- `HOME=/tmp`, which any UID can write.

```dockerfile
FROM ghost:5-alpine
ARG WITH_S3=0
USER root
RUN echo "pico:x:10001:10001::/var/lib/ghost:/sbin/nologin" >> /etc/passwd && \
    cd /var/lib/ghost && cp -a content.orig/. content/ && \
    chown -R 10001:10001 /var/lib/ghost/content
COPY docker-entrypoint.sh /pico-entrypoint.sh
RUN chmod +x /pico-entrypoint.sh
ENV HOME=/tmp
USER 10001:10001
ENTRYPOINT ["/pico-entrypoint.sh"]
```

This is abridged; the S3 adapter install is in the real file. The final image is about 185 MiB.

### The entrypoint shim

PicoPaaS env keys must match `^[A-Z_][A-Z0-9_]*$`, but Ghost wants lowercase, `__`-nested keys such as
`database__connection__host`. The entrypoint translates, then `exec`s Ghost:

- `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_NAME` become `database__connection__*`.
- `SITE_URL` becomes `url`.
- `DB_CA_CERT` becomes `database__connection__ssl__ca`, so MySQL TLS is verified.
- `S3_*` becomes the S3 storage config. `assetHost` is set to the public Spaces URL
  (`https://<bucket>.<region>.digitaloceanspaces.com`) so image links resolve. The adapter's default is an AWS-style URL, which is wrong for DigitalOcean Spaces.
- `security__staffDeviceVerification=false`. PicoPaaS blocks SMTP, so Ghost's sign-in verification email can't
  send and would lock you out of the admin.

### Deploy on PicoPaaS

Deploy from the dashboard (**New app**, filling in the fields below), or from the CLI / `curl` with a YAML spec.
The spec is the whole app: `name`, `image`, `port`, `resources` (database + bucket), and `env` — where you map
the platform refs onto the names Ghost's entrypoint expects.

**`ghost-blog.yaml`**

```yaml
apiVersion: picopaas/v1
name: ghost-blog
image: ghcr.io/seaburr/picopaas-example-ghost:latest
port: 2368
resources:
  db: true
  storage: true
env:
  DB_HOST: "${{ db.host }}"
  DB_PORT: "${{ db.port }}"
  DB_NAME: "${{ db.name }}"
  DB_USER: "${{ db.user }}"
  DB_PASSWORD: "${{ db.password }}"
  DB_CA_CERT: "${{ db.ca_cert }}"
  S3_ENDPOINT: "${{ storage.endpoint }}"
  S3_REGION: "${{ storage.region }}"
  S3_BUCKET: "${{ storage.bucket }}"
  S3_ACCESS_KEY: "${{ storage.access_key }}"
  S3_SECRET_KEY: "${{ storage.secret_key }}"
  SITE_URL: "${{ app.url }}"
```

Quote the `${{ ... }}` values — the braces aren't valid unquoted YAML. No `secrets:` block is needed here:
every value is a platform ref, not a literal secret. (`${{ app.url }}` resolves to this app's own generated URL,
so there's no second step to set it; `${{ db.* }}` / `${{ storage.* }}` are injected from the database and bucket
you requested under `resources`.)

Get an API key from the **API keys** page and export it:

```bash
export PICOPAAS_TOKEN=pp_sk_...
```

**CLI:**

```bash
picopaas apply -f ghost-blog.yaml
```

**curl** (the `/apps/spec` endpoint takes `{"spec": "<yaml>"}`; `jq -Rs` wraps the file for you):

```bash
curl -fsS -X POST https://platform.picopaas.xyz/apps/spec \
  -H "Authorization: Bearer $PICOPAAS_TOKEN" \
  -H "Content-Type: application/json" \
  --data "$(jq -Rs '{spec: .}' ghost-blog.yaml)"
```

Both return the created app with its generated `https://<name>.picopaas.xyz` hostname. It deploys in
one step (no URL to set afterward); open `/ghost/` there to finish setup. Re-running `apply` with the same `name`
updates the app in place.

Posts and settings live in MySQL and uploaded images in the bucket, so both survive restarts.

## Lessons for bringing your own image

- **Run as a non-root numeric UID.** PicoPaaS forces UID 10001. If the stock image uses a root entrypoint, build
  a wrapper that does the root-time setup at build and runs as the UID.
- **Add an `/etc/passwd` entry for the UID.** Python 3.13 `getpass.getuser()` and Node `os.userInfo()` throw for a
  UID with no entry. Also set `HOME=/tmp`.
- **Env keys are UPPER_SNAKE.** If your app wants lowercase or nested config, translate in an entrypoint.
- **Use the refs:** `${{ app.url }}`, `${{ db.host }}`, `${{ db.port }}`, `${{ db.name }}`, `${{ db.user }}`,
  `${{ db.password }}`, `${{ db.ca_cert }}`, `${{ storage.endpoint }}`, `${{ storage.region }}`,
  `${{ storage.bucket }}`, `${{ storage.access_key }}`, `${{ storage.secret_key }}`. See the
  [User guide](USER_GUIDE.md) and [API](API.md) for the full set.
- **No persistent filesystem.** The 250 MB scratch disk is wiped on restart. Keep state in the MySQL database and
  uploads in the storage bucket, and set your S3 client's public URL or asset host to the bucket's public Spaces URL.
- **No outbound SMTP.** Disable features that send email, or they will error.
- **Fit the caps:** 512 MB image, 200m CPU, 384 MB RAM, 50 MB database, 100 MB bucket, 250 MB scratch disk.
