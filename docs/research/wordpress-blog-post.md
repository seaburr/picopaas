Excerpt: We asked what it would take to run a real WordPress blog on PicoPaaS. The answer is yes, with a custom image and a few habits, and no persistent volume needed to start.

# Could WordPress run on PicoPaaS? A paper test

Chas wants his personal blog to live on the platform he built. That is a good test, because WordPress is the
opposite of a tidy twelve-factor app. It writes to its own folder, loves cron, sends email and runs on PHP. So before
we touch anything, here is what a paper review says.

No infrastructure was changed for this. Everything below is research, and a few points are marked as "needs proof
on a throwaway app" in the notes.

## The short answer

Yes, with one replica, a custom image, MySQL for content and a Spaces bucket for media. We do not need persistent
volumes for the first version.

## What gets in the way

**The official image runs as root.** Apache starts as root and then drops to `www-data`. Our pods run as a numeric
non-root user with every Linux capability dropped, so the stock image will not start. The fix is the same one the
Ghost recipe uses: a small wrapper image that listens on 8080, runs as the non-root user and has the core files
already in place.

**Files do not survive a restart.** The scratch disk is 250 MB and wiped when the pod restarts. WordPress wants to
write plugins, themes and uploads there. Our answer is to stop letting it. Plugins and themes are baked into the image,
file edits from the dashboard are turned off, and updates happen by rebuilding the image. Media goes to the Spaces
bucket through an offload plugin. The database holds everything else. The auth salts get pinned as secrets so the
entrypoint does not invent new ones, and log everybody out, on every restart.

**Cron needs HTTP, and our own URL is awkward from inside.** WP-Cron calls the site's public address from within
the pod, and calls from the cluster to its own public hostname hit the load balancer hairpin problem we already
documented. The wrapper turns off WP-Cron and runs the PHP script from a small loop instead.

**No outbound SMTP.** Ports 25, 465 and 587 are blocked on purpose. Mail goes through a provider's HTTPS API plugin.

**CPU is the real constraint.** An uncached WordPress page burns real CPU time, and at 200m that is only a page or
two per second before throttling. We saw exactly this with Ghost, where roughly 200 ms of CPU per page throttled the
pod until we raised the limit. WordPress needs opcache, a static page cache and ideally Cloudflare in front of the
custom domain. With that, most visitors never reach PHP.

**Workers have to fit in 384 MB.** A PHP worker with WordPress is around 40 to 60 MB. We limit Apache to five workers,
which also keeps us under the ten-connection database cap, because PHP opens a connection per busy worker instead of
pooling.

## Limits a real blog will hit

The defaults are tuned for throwaway projects. A blog with a few years of images will cross several.

| Limit | Default | Suggested override |
|---|---|---|
| CPU | 200m | 500m |
| Database | 50 MB | 256 MB |
| Bucket | 100 MB | 2 GB |
| Scratch disk | 250 MB | 512 MB if the page cache lives on disk |

These are all per-app overrides an admin can set, so no platform change is needed. They are also why the platform
keeps the defaults small and the overrides cheap.

## What about volumes?

We have a plan for opt-in block storage volumes: one small DigitalOcean volume per app, mounted into the pod.
At $0.10 per GiB per month with a 1 GiB minimum, a volume costs about ten cents a month, and each node can attach 15
of them. WordPress would make a good first user, but it also shows what the plan is missing: the mount path has to be
configurable (a mount over `wp-content` hides the plugins in the image), a volume app is single-replica and every deploy
is a short outage, and there are no backups yet.

So the plan is two phases. Phase 1 ships with no volume at all. Phase 2 adds volumes only if the dashboard
workflow turns out to matter more than we think.

## Moving the blog in

Two paths. A WordPress export and import carries posts, pages, comments and media references, and is the cleanest
option if the old database has cruft. A database dump plus an uploads copy keeps everything exactly, but we have no
supported way to run a one-off import against an app's database yet. That is a gap worth closing, because every
migration will want it.

## What it tells us about the platform

WordPress hits the same walls as most recipes: root-only images, blocked mail, no job runner, CPU for dynamic sites,
and no backup story. Splitting state into a database, a bucket and an immutable image gets surprisingly far without
volumes. We should say so in the docs, and treat volumes as an escape hatch. The honest pitch is a personal blog or
a low-traffic site, not a plugin-heavy shop.

Next step: build the wrapper image on a throwaway app, measure the CPU and memory numbers, and then decide what goes
into a recipe.
