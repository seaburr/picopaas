# Ghost on PicoPaaS: a real, persistent blog within the platform's constraints.
# - Runs as a non-root numeric UID (PicoPaaS forces 10001); the stock Ghost image starts as root and
#   su-exec's down, which PicoPaaS rejects — so we drop to a fixed UID at build and skip the root steps.
# - Content lives in PicoPaaS MySQL; uploaded images go to the PicoPaaS S3 bucket (both survive restarts).
# - ghost:5-alpine is ~168 MB compressed, under the 256 MB image cap. Keep added deps small to stay under.
FROM ghost:5-alpine

USER root
RUN set -eux; \
    # Node's os.userInfo() throws for a UID with no /etc/passwd entry (the Node analog of Python's
    # getpass.getuser() crash). Give 10001 a passwd entry so Ghost/its deps don't fall over.
    if ! getent passwd 10001 >/dev/null 2>&1; then \
      echo "pico:x:10001:10001::/var/lib/ghost:/sbin/nologin" >> /etc/passwd; \
    fi; \
    # S3 storage adapter for uploaded images -> content/adapters/storage/s3 (where Ghost loads adapters).
    cd /var/lib/ghost; \
    npm install --no-save ghost-storage-adapter-s3@3.0.1; \
    mkdir -p content/adapters/storage/s3; \
    cp -a node_modules/ghost-storage-adapter-s3/. content/adapters/storage/s3/; \
    chown -R 10001:10001 /var/lib/ghost

COPY docker-entrypoint.sh /pico-entrypoint.sh
RUN chmod +x /pico-entrypoint.sh

ENV HOME=/var/lib/ghost
WORKDIR /var/lib/ghost
USER 10001:10001
EXPOSE 2368
ENTRYPOINT ["/pico-entrypoint.sh"]
