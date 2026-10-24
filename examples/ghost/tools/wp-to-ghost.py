#!/usr/bin/env python3
"""Import published WordPress posts (and pages) into Ghost using only public WP content and the Ghost Admin API.

Source:  WordPress public REST API (no login). Plain-permalink sites work: the script uses ?rest_route=.
Target:  Ghost Admin API with a Custom Integration key (Settings > Integrations > Add custom integration).

Usage:
  export WP_URL=https://old-site.example         # or --wp
  export GHOST_URL=https://new-site.example      # or --ghost
  export GHOST_ADMIN_KEY=<id>:<secret>           # or --key-file FILE (KEY=VALUE lines, GHOST_ADMIN_KEY / GHOST_POC_ADMIN_KEY)
  python3 wp-to-ghost.py --dry-run               # fetch + report, touch nothing in Ghost
  python3 wp-to-ghost.py                         # import (idempotent: existing slugs are skipped)

Options: --no-pages, --skip-tag NAME (repeatable, default "Uncategorized"), --redirects FILE (write a Ghost
redirects.json for old pretty permalinks (/2020/11/slug/ -> /slug/); ?p=N URLs cannot be redirected by Ghost).
Standard library only. Never prints the key.
"""
import argparse, base64, hashlib, hmac, html, json, mimetypes, os, re, sys, time, uuid
import urllib.error, urllib.parse, urllib.request

UA = {"User-Agent": "wp-to-ghost/1.0"}


def http(url, method="GET", headers=None, data=None, retries=3):
    h = dict(UA); h.update(headers or {})
    for i in range(retries):
        try:
            req = urllib.request.Request(url, data=data, method=method, headers=h)
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            body = e.read()
            if e.code >= 500 and i < retries - 1:
                time.sleep(2 * (i + 1)); continue
            return e.code, body, dict(e.headers)
        except (urllib.error.URLError, TimeoutError):
            if i == retries - 1: raise
            time.sleep(2 * (i + 1))


# ---------- WordPress ----------
def wp_get(base, route, **params):
    q = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
    url = f"{base}/?rest_route={route}" + (f"&{q}" if q else "")
    st, body, hd = http(url)
    if st != 200:
        raise SystemExit(f"WP {route} -> HTTP {st}")
    return json.loads(body), hd


def wp_all(base, route, **params):
    out, page = [], 1
    while True:
        data, hd = wp_get(base, route, per_page=100, page=page, **params)
        out += data
        if page >= int(hd.get("X-WP-TotalPages", hd.get("x-wp-totalpages", 1))): return out
        page += 1


# ---------- Ghost ----------
class Ghost:
    def __init__(self, base, key):
        self.base = base.rstrip("/")
        self.kid, secret = key.split(":", 1)
        self.secret = bytes.fromhex(secret)

    def _jwt(self):
        b = lambda x: base64.urlsafe_b64encode(x).rstrip(b"=")
        now = int(time.time())
        head = b(json.dumps({"alg": "HS256", "typ": "JWT", "kid": self.kid}).encode())
        pay = b(json.dumps({"iat": now, "exp": now + 300, "aud": "/admin/"}).encode())
        sig = b(hmac.new(self.secret, head + b"." + pay, hashlib.sha256).digest())
        return (head + b"." + pay + b"." + sig).decode()

    def call(self, method, path, body=None, raw=None, ctype=None):
        h = {"Authorization": f"Ghost {self._jwt()}", "Accept-Version": "v5.0"}
        data = raw
        if body is not None:
            data = json.dumps(body).encode(); h["Content-Type"] = "application/json"
        if ctype: h["Content-Type"] = ctype
        st, resp, _ = http(f"{self.base}/ghost/api/admin/{path}", method, h, data, retries=2)
        try: j = json.loads(resp)
        except ValueError: j = {"raw": resp[:200].decode("utf-8", "replace")}
        return st, j

    def exists(self, kind, slug):
        st, _ = self.call("GET", f"{kind}/slug/{urllib.parse.quote(slug)}/")
        return st == 200

    def upload_image(self, name, content):
        bd = uuid.uuid4().hex
        ct = mimetypes.guess_type(name)[0] or "application/octet-stream"
        body = (f'--{bd}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\nContent-Type: {ct}\r\n\r\n').encode() \
            + content + (f'\r\n--{bd}\r\nContent-Disposition: form-data; name="purpose"\r\n\r\nimage\r\n--{bd}--\r\n').encode()
        st, j = self.call("POST", "images/upload/", raw=body, ctype=f"multipart/form-data; boundary={bd}")
        if st not in (200, 201): raise RuntimeError(f"image upload {name}: HTTP {st} {str(j)[:200]}")
        return j["images"][0]["url"]


def excerpt(wp_excerpt):
    t = html.unescape(re.sub(r"<[^>]+>", " ", wp_excerpt or ""))
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"\s*(\[…\]|\[&hellip;\]|Continue reading.*)$", "", t).strip()
    if len(t) > 300:
        t = t[:297].rsplit(" ", 1)[0].rstrip(",;:-") + "..."
    return t


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--wp", default=os.environ.get("WP_URL"))
    ap.add_argument("--ghost", default=os.environ.get("GHOST_URL"))
    ap.add_argument("--key-file")
    ap.add_argument("--no-pages", action="store_true")
    ap.add_argument("--skip-tag", action="append", default=None)
    ap.add_argument("--redirects")
    ap.add_argument("--dry-run", action="store_true", help="no Ghost calls; fetch, rewrite, report")
    ap.add_argument("--out", help="dry-run: directory for payload JSON files and summary.json")
    a = ap.parse_args()
    skip_tags = {s.lower() for s in (a.skip_tag or ["Uncategorized"])}
    key = os.environ.get("GHOST_ADMIN_KEY")
    if a.key_file:
        for line in open(os.path.expanduser(a.key_file)):
            k, _, v = line.strip().partition("=")
            if k in ("GHOST_ADMIN_KEY", "GHOST_POC_ADMIN_KEY") and v: key = v.strip("'\"")
            if k in ("GHOST_URL", "GHOST_POC_URL") and v and not a.ghost: a.ghost = v.strip("'\"")
    if not a.wp: sys.exit("need --wp or WP_URL")
    wp = a.wp.rstrip("/")
    if not a.dry_run:
        if not (a.ghost and key): sys.exit("need --ghost/GHOST_URL and a key (GHOST_ADMIN_KEY or --key-file)")
        g = Ghost(a.ghost, key); gbase = a.ghost.rstrip("/")
    else:
        g, gbase = None, (a.ghost or "https://ghost.invalid").rstrip("/")

    posts = wp_all(wp, "/wp/v2/posts", status="publish")
    pages = [] if a.no_pages else wp_all(wp, "/wp/v2/pages", status="publish")
    cats = {c["id"]: c["name"] for c in wp_all(wp, "/wp/v2/categories")}
    tags = {t["id"]: t["name"] for t in wp_all(wp, "/wp/v2/tags")}
    print(f"WP: {len(posts)} posts, {len(pages)} pages")

    idmap = {}  # WP id -> ("post"|"page", slug)
    for p in posts: idmap[("p", p["id"])] = p["slug"]
    for p in pages: idmap[("page_id", p["id"])] = p["slug"]
    host = urllib.parse.urlparse(wp).netloc
    media_cache, img_cache = {}, {}
    stats = dict(created=0, skipped=0, images=0, failed=0)
    redirects, summary = [], []

    def ghost_image(url):
        if url in img_cache: return img_cache[url]
        if g is None:
            st, content, _ = http(url)
            if st != 200: print(f"  ! image {url} -> HTTP {st}")
            img_cache[url] = url; return url
        st, content, _ = http(url)
        if st != 200:
            print(f"  ! image {url} -> HTTP {st}, left as is"); img_cache[url] = url; return url
        new = g.upload_image(os.path.basename(urllib.parse.urlparse(url).path), content)
        stats["images"] += 1; img_cache[url] = new; return new

    def rewrite(h):
        h = re.sub(r'\s(?:srcset|sizes)="[^"]*"', "", h)
        up = re.compile(r'https?://' + re.escape(host) + r'(/wp-content/uploads/[^"\'\s<>)]+)')
        h = up.sub(lambda m: ghost_image(m.group(0)), h)
        def link(m):
            q = urllib.parse.parse_qs(m.group(2))
            for k in ("p", "page_id"):
                if k in q and (k, int(q[k][0])) in idmap:
                    return f'href="{gbase}/{idmap[(k, int(q[k][0]))]}/"'
            return m.group(0)
        return re.sub(r'href="(https?://' + re.escape(host) + r'/?)\?([^"#]*)(?:#[^"]*)?"', link, h)

    def featured(p):
        mid = p.get("featured_media")
        if not mid: return None
        if mid not in media_cache:
            st, body, _ = http(f"{wp}/?rest_route=/wp/v2/media/{mid}")
            media_cache[mid] = json.loads(body).get("source_url") if st == 200 else None
        u = media_cache[mid]
        return ghost_image(u) if u else None

    for kind, items in (("posts", posts), ("pages", pages)):
        for p in items:
            slug = p["slug"]; title = html.unescape(re.sub(r"<[^>]+>", "", p["title"]["rendered"]))
            idk = ("p", p["id"]) if kind == "posts" else ("page_id", p["id"])
            lk = urllib.parse.urlparse(p.get("link", ""))
            if lk.path.strip("/") and not lk.query and lk.path.strip("/") != slug:  # pretty-permalink sites only
                redirects.append({"from": "^" + re.escape(lk.path.rstrip("/")) + "/?$", "to": f"/{slug}/", "permanent": True})
            if g and g.exists(kind, slug):
                stats["skipped"] += 1; print(f"skip {kind[:-1]} {slug}"); continue
            names, seen = [], set()
            for n in [cats.get(c) for c in p.get("categories", [])] + [tags.get(t) for t in p.get("tags", [])]:
                if n and n.lower() not in skip_tags and n.lower() not in seen:
                    seen.add(n.lower()); names.append({"name": html.unescape(n)})
            obj = {"title": title, "slug": slug, "status": "published",
                   "published_at": p["date_gmt"] + ".000Z", "updated_at": p["modified_gmt"] + ".000Z",
                   "html": rewrite(p["content"]["rendered"]), "tags": names}
            ex = excerpt(p.get("excerpt", {}).get("rendered"))
            if ex: obj["custom_excerpt"] = ex
            if kind == "posts":
                fi = featured(p)
                if fi: obj["feature_image"] = fi
            if g is None:
                print(f"dry {kind[:-1]} {slug} tags={[t['name'] for t in names]}")
                body = obj["html"]
                flags = [f for f, rx in (("shortcode", r"\[[a-z_-]+(?: [^\]]*)?\]"), ("gallery", r"wp-block-gallery|gallery-item"),
                         ("image-compare", r"image-compare"), ("iframe", r"<iframe"), ("table", r"<table")) if re.search(rx, body)]
                links = len(re.findall(r'href="' + re.escape(gbase) + r'/[a-z0-9-]+/"', body))
                summary.append({"slug": slug, "kind": kind[:-1], "tags": [t["name"] for t in names], "internal_links": links,
                                "images": len(re.findall(r"<img\b", body)), "feature_image": bool(obj.get("feature_image")), "flags": flags})
                if a.out:
                    os.makedirs(a.out, exist_ok=True)
                    json.dump({kind: [obj]}, open(os.path.join(a.out, f"{kind[:-1]}-{slug}.json"), "w"), indent=1)
                continue
            st, j = g.call("POST", f"{kind}/?source=html", {kind: [obj]})
            if st in (200, 201):
                stats["created"] += 1; print(f"ok   {kind[:-1]} {slug}")
            else:
                stats["failed"] += 1; print(f"FAIL {kind[:-1]} {slug}: HTTP {st} {str(j)[:300]}")
    if a.redirects:
        json.dump(redirects, open(a.redirects, "w"), indent=1)
        print(f"wrote {len(redirects)} redirects to {a.redirects}")
    if summary:
        tot = {"posts": sum(x["kind"] == "post" for x in summary), "pages": sum(x["kind"] == "page" for x in summary),
               "unique_images": len(img_cache), "internal_link_rewrites": sum(x["internal_links"] for x in summary),
               "flagged": {x["slug"]: x["flags"] for x in summary if x["flags"]}}
        print(json.dumps(tot, indent=1))
        if a.out: json.dump({"totals": tot, "items": summary}, open(os.path.join(a.out, "summary.json"), "w"), indent=1)
    print(stats)
    sys.exit(1 if stats["failed"] else 0)


if __name__ == "__main__":
    main()
