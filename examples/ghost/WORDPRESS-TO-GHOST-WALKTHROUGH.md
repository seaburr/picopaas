# Walkthrough: import the WordPress posts into a Ghost proof of concept

Hands-on steps. Generic reference: [MIGRATING-FROM-WORDPRESS.md](MIGRATING-FROM-WORDPRESS.md). The script is
[tools/wp-to-ghost.py](tools/wp-to-ghost.py) (Python 3, standard library only). Run everything from the repo root.

Source used below: the restored WordPress at `https://lucky-reindeer-zoijlus.picopaas.xyz` (public REST API, only public content).
Verified against a local Ghost 5: 78 posts + 2 pages, 17 images, all created; a second run skipped all 80.

## a. Write the spec and deploy
```
cat > ghost-poc.yaml <<'EOF'
apiVersion: picopaas/v1
name: ghost-poc
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
EOF
export PICOPAAS_URL=https://platform.picopaas.xyz PICOPAAS_TOKEN=<your token>
python3 cli/picopaas.py apply -f ghost-poc.yaml
```
You should see `Created ghost-poc (queued). It'll be live in about a minute at https://<host>.picopaas.xyz`.
(An app named `ghost-poc` may already exist, id 153, deployed ahead of time. Then `apply` prints `Updated`; that is fine.)

## b. Wait for running
```
python3 cli/picopaas.py status ghost-poc
curl -s https://<host>.picopaas.xyz/ghost/api/admin/authentication/setup/
```
Wait for `status: running`. The curl should print `{"setup":[{"status":false}]}` (setup not done yet).

## c. First-run setup (you do this in the browser)
Open `https://<host>.picopaas.xyz/ghost/`, create the owner account (site title, your name, email, a strong password).
Staff device verification is already turned off by the image (`security__staffDeviceVerification=false`), because PicoPaaS blocks
outbound SMTP. If you are still asked for a verification code on sign-in, it means you are running a different image: use the
`ghcr.io/seaburr/picopaas-example-ghost` image from the spec above. Expect the Ghost default "Coming soon" post and sample pages
to exist; delete them in the admin (Posts) so counts match later.

## d. Create the custom integration and save the key
1. Ghost admin > Settings > Integrations > **Add custom integration**, name it `wp-import`.
2. Copy the **Admin API key** (looks like `<24 hex>:<64 hex>`).
3. Save it, readable only by you:
```
umask 077
cat > ~/.picopaas-ghost-poc.env <<'EOF'
GHOST_POC_URL=https://<host>.picopaas.xyz
GHOST_POC_ADMIN_KEY=<id>:<secret>
EOF
```
The script reads `GHOST_POC_*` or `GHOST_*` names from `--key-file`.

## e. Dry run (touches nothing in Ghost)
```
python3 examples/ghost/tools/wp-to-ghost.py --wp https://lucky-reindeer-zoijlus.picopaas.xyz --dry-run --out /tmp/ghost-dry
```
Expect a `dry post <slug> tags=[...]` line per post, then totals: 78 posts, 2 pages, 17 unique images, 0 internal `?p=` link rewrites
(the source has none), and a `flagged` list: `using-terraform-to-manage-digitalocean-resources` (image-compare block),
`re-architecting-this-website-v` (gallery), `xps-life` (table), and four posts with YouTube/Vimeo iframes. Payloads are in
`/tmp/ghost-dry/*.json`, totals in `/tmp/ghost-dry/summary.json`.

Decision points (decide before the real run):
- **Tags**: categories and tags both become Ghost tags (about 20). To drop the noisy ones, delete them in Ghost afterwards, or pass
  `--skip-tag NAME` (repeatable; `Uncategorized` is skipped by default).
- **Pages**: `about-me` and `killallbindings` are imported as pages. Use `--no-pages` to skip them.
- **Featured images**: only posts that had a WordPress featured image get one (one post). Images inside posts are imported either way.
- **Authors**: all posts will be yours (the WordPress display name `cb` is not carried over).
- **Flagged posts** (above): imported as the HTML WordPress rendered; plan to eyeball them.

## f. Real import
```
python3 examples/ghost/tools/wp-to-ghost.py --wp https://lucky-reindeer-zoijlus.picopaas.xyz --key-file ~/.picopaas-ghost-poc.env
```
Expect `ok post <slug>` lines and a final `{'created': 80, 'skipped': 0, 'images': 17, 'failed': 0}`. Re-running is safe: existing slugs
are skipped (`skip post <slug>`), nothing is updated or duplicated. A `FAIL` line shows the HTTP status; fix and re-run.

## g. Verify
```
HOST=https://<host>.picopaas.xyz
# 1. post count: Ghost admin > Posts (filter Published) should show 78 (+ the 2 pages under Pages)
# 2. every post URL answers 200 (slugs from the source):
curl -s "https://lucky-reindeer-zoijlus.picopaas.xyz/?rest_route=/wp/v2/posts&per_page=100&_fields=slug" \
  | python3 -c "import sys,json;[print(p['slug']) for p in json.load(sys.stdin)]" \
  | while read s; do printf '%s %s\n' "$(curl -s -o /dev/null -w '%{http_code}' $HOST/$s/)" "$s"; done | sort | uniq -c -w3
# 3. images are served from blob storage, not WordPress:
curl -s $HOST/creating-a-dynamic-inventory-script-for-ansible/ | grep -o '<img[^>]*src="[^"]*"' | head -3
curl -s $HOST/ | grep -o 'src="[^"]*digitaloceanspaces[^"]*"' | head -3
# 4. code blocks keep their line breaks (this post has a 113-line block):
curl -s $HOST/creating-a-dynamic-inventory-script-for-ansible/ | python3 -c "import sys,re;m=re.search(r'<pre.*?</pre>',sys.stdin.read(),re.S);print(m.group(0).count(chr(10)),'newlines (expect ~113)')"
# 5. tags exist: Ghost admin > Tags, or open $HOST/tag/ansible/
# 6. original dates kept: $HOST/broadcom-does-it-again/ shows 25 Jul 2025
```
Expected: 200 for all slugs, image `src` values on `https://<bucket>.<region>.digitaloceanspaces.com/...`, code block newlines intact.
Eyeball the flagged posts from step e. Memory/CPU: `python3 cli/picopaas.py status ghost-poc` and the app page in the dashboard.

## h. Optional: redirects from old URLs
`?p=N` URLs cannot be redirected by Ghost (it matches paths only; tested). Links between the imported posts are rewritten already. If the
old site ever used pretty permalinks (`/2020/11/slug/`), run the script with `--redirects redirects.json` and upload that file in
Ghost admin > Settings > Labs > Redirects. For this source the file is empty.

## i. Cleanup
```
python3 cli/picopaas.py delete ghost-poc
rm ~/.picopaas-ghost-poc.env
```
(Deleting the app removes its database and bucket.) Also delete the `wp-import` integration in Ghost if you keep the site.
