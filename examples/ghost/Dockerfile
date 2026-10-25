# Ghost on PicoPaaS: a real blog within the platform's constraints.
# - Runs as a non-root numeric UID (PicoPaaS forces 10001); the stock Ghost image starts as root and
#   su-exec's down, which PicoPaaS rejects — so we drop to a fixed UID at build and skip the root steps.
# - Content lives in PicoPaaS MySQL (survives restarts). ghost:N-alpine is ~170 MB compressed, under the cap.
# - WITH_S3=1 adds the S3 storage adapter so uploaded images persist in the PicoPaaS bucket. That pulls in
#   aws-sdk v2, which (even pruned) pushes the image near/over the 256 MB cap, so it is OFF by default; the
#   lean default image stores uploads on the ephemeral disk (lost on restart) but keeps all content in MySQL.
# Ghost 6 only (the Ghost 5 build was retired after both blogs were cut over; see README).
FROM ghost:6-alpine

ARG WITH_S3=0

USER root
RUN set -eux; \
    # Node's os.userInfo() throws for a UID with no /etc/passwd entry (the Node analog of Python's
    # getpass.getuser() crash); give 10001 a passwd entry.
    if ! getent passwd 10001 >/dev/null 2>&1; then \
      echo "pico:x:10001:10001::/var/lib/ghost:/sbin/nologin" >> /etc/passwd; \
    fi; \
    cd /var/lib/ghost; \
    if [ "$WITH_S3" = "1" ]; then \
      npm install --no-save ghost-storage-adapter-s3@2.8.0; \
      mkdir -p content/adapters/storage/s3; \
      cp -a node_modules/ghost-storage-adapter-s3/. content/adapters/storage/s3/; \
      # Slim aws-sdk v2: keep only the S3 + STS api specs, drop the browser bundle, TS defs and docs.
      A=node_modules/aws-sdk; \
      find "$A/apis" -type f ! -name 's3-*' ! -name 'sts-*' ! -name 'metadata.json' -delete; \
      rm -rf "$A/dist" "$A/.changes" "$A/scripts"; \
      find "$A" -name '*.d.ts' -delete; \
      find "$A" -name '*.md' -delete; \
    fi; \
    # The stock entrypoint seeds content/ (default themes incl. the active "source" theme) from content.orig
    # on first run; we bypass it, and content/ is ephemeral anyway, so bake the defaults into the image.
    cp -a content.orig/. content/; \
    # Only content/ must be writable by the runtime UID. chown of the WHOLE tree would duplicate all of
    # Ghost into a second image layer (~+100 MB); the code dirs stay root-owned and world-readable.
    chown -R 10001:10001 /var/lib/ghost/content; \
    # Perf patches (each fails the build if Ghost moves the anchor). Pods cannot reach their own public URL
    # (LB hairpin, docs/PENTEST.md #11), and Ghost probes the site's own favicon/logo URL on EVERY page
    # render, so each render waited out the 5000 ms image-size timeout (overrides.json cannot be set via env).
    # Dimensions are only decoration (structured data), so fail fast: 400 ms.
    C=/var/lib/ghost/current/core; \
    grep -q '"getImageSizeTimeoutInMS": 5000' $C/shared/config/overrides.json; \
    sed -i 's/"getImageSizeTimeoutInMS": 5000/"getImageSizeTimeoutInMS": 400/' $C/shared/config/overrides.json; \
    grep -q '"getImageSizeTimeoutInMS": 400' $C/shared/config/overrides.json; \
    # Outbound webmentions (sent on publish) fetch every linked URL with a 15 s timeout x3 retries; links to
    # other apps on this platform hang on the same hairpin. Receiving webmentions is unaffected.
    grep -q "isEnabled: () => !settingsCache.get('is_private')," $C/server/services/mentions/service.js; \
    sed -i "s/isEnabled: () => !settingsCache.get('is_private'),/isEnabled: () => false,/" $C/server/services/mentions/service.js; \
    grep -q 'isEnabled: () => false,' $C/server/services/mentions/service.js

COPY docker-entrypoint.sh /pico-entrypoint.sh
RUN chmod +x /pico-entrypoint.sh

# HOME must be writable by UID 10001 (Ghost/CLI may write dotfiles); /tmp is world-writable.
ENV HOME=/tmp
# Config defaults for every Ghost app (override per app only if you must; UPPER_SNAKE app env cannot set these):
# no update check / RPC ping (outbound calls, useless on an immutable image), and 2 min browser caching of HTML
# (Ghost default 0 = revalidate every view; theme assets are already cached 1 year).
ENV privacy__useUpdateCheck=false \
    privacy__useRpcPing=false \
    caching__frontend__maxAge=120
WORKDIR /var/lib/ghost
USER 10001:10001
EXPOSE 2368
ENTRYPOINT ["/pico-entrypoint.sh"]
