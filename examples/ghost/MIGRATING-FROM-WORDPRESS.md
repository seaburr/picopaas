# Migrating a WordPress blog to Ghost on PicoPaaS

Approach: read the WordPress site's public REST API, upload every referenced image to Ghost, and create each post through the
Ghost Admin API. No WordPress login, database dump or Ghost staff session is needed. (Ghost's `/db/` JSON importer needs a staff
session, so it is not used.)

## Steps
1. Deploy Ghost from this folder's README (database + blob storage, so images persist). Open `/ghost/` and finish the first-run setup
   yourself (owner account is yours to create).
2. In Ghost: Settings > Integrations > Add custom integration. Copy the **Admin API key** (`<id>:<secret>`).
3. Keep the key out of shell history and the repo, for example in a file only you can read:
   ```
   GHOST_URL=https://your-ghost.example
   GHOST_ADMIN_KEY=<id>:<secret>
   ```
4. Run (Python 3, standard library only):
   ```
   python3 examples/ghost/tools/wp-to-ghost.py --wp https://old-site.example --key-file ~/ghost.env --dry-run
   python3 examples/ghost/tools/wp-to-ghost.py --wp https://old-site.example --key-file ~/ghost.env --redirects redirects.json
   ```
   Arguments can also come from `WP_URL`, `GHOST_URL`, `GHOST_ADMIN_KEY`. Other flags: `--no-pages`, `--skip-tag NAME` (repeatable;
   default skips the `Uncategorized` category).

The script is idempotent: a post or page whose slug already exists in Ghost is skipped, never updated. To re-import a post, delete it in
Ghost first. Images are re-uploaded on each run for posts that are (re)created.

## What migrates
- Published posts and pages: title, slug, `published_at` (original date, GMT), modified date, rendered HTML body, excerpt (HTML stripped,
  at most 300 characters, becomes Ghost's custom excerpt), featured image.
- Categories and tags both become Ghost tags (deduplicated by name).
- Images: every `wp-content/uploads` URL in content, links and featured images is downloaded and uploaded to Ghost (so it lands in the
  blob storage bucket) and the URL is rewritten. `srcset`/`sizes` attributes are dropped.
- Internal links of the form `?p=N` or `?page_id=N` become `/<slug>/` on the Ghost site.
- `<pre><code>` blocks are preserved (Ghost turns them into code cards).

## What does not migrate
- **Authors**: posts are created as the integration's owner. Rename or add authors in Ghost afterwards.
- **Comments**: Ghost has none built in. Use Ghost's native comments (members + email, which needs mail) or a third-party widget.
- **Plugins and shortcodes**: raw `[shortcode]` text and plugin blocks (galleries, image-compare, forms, embeds from plugins) come through as
  plain HTML or not at all. Review posts that used them; the HTML that WordPress rendered is what is imported.
- **Embeds**: iframes (YouTube, Vimeo) are imported as HTML; check they render and allow framing.
- **Drafts, private posts, revisions, menus, widgets, theme and SEO plugin metadata.**
- **Non-media links** to files outside `wp-content/uploads` (for example static folders served beside WordPress) keep their old, now
  possibly dead, URL. Search the imported content for the old hostname.
- **Pretty-permalink URLs**: if the old site used `/year/month/slug/` the new URLs are `/<slug>/`; redirects are needed (below).

## Redirects from old URLs
- `?p=N` / `?page_id=N` URLs: **Ghost cannot redirect these.** Its redirects match the request path only, not the query string
  (tested on Ghost 5: `/?p=437` returned the home page with a 200 even with a matching rule loaded). Internal links inside the imported
  posts are rewritten, so only outside links to old `?p=` URLs break. Fix them in front of Ghost if it matters.
- Pretty permalinks (`/2020/11/slug/`): `--redirects FILE` writes a Ghost `redirects.json` (regex `from`, `to: /<slug>/`, permanent). A
  site whose permalinks are plain `?p=N` gets an empty file. Upload under Settings > Labs > Redirects (a staff action; the integration key
  cannot do it), then test with `curl -I https://your-ghost.example/2020/11/slug/`.
