# Backup image (docs/design/backups.md): restic + DB clients, used by the platform's backup Jobs in ns picopaas-backup.
# Published as ghcr.io/seaburr/picopaas-backup (.github/workflows/backup-image.yml). Carries no secrets; all
# credentials arrive as env vars from a per-run Secret. Runs as a non-root UID with a read-only root filesystem.
ARG ALPINE_VERSION=3.22

FROM alpine:${ALPINE_VERSION} AS restic
# Pinned release + checksum (from the release's SHA256SUMS); the build fails if the download differs.
ARG RESTIC_VERSION=0.19.1
ARG RESTIC_SHA256=f415415624dcc452f2a02b8c33641791a8c6d6d3b65bbb3543fcf9a25151585c
RUN apk add --no-cache curl bzip2 \
 && curl -fsSL -o /restic.bz2 "https://github.com/restic/restic/releases/download/v${RESTIC_VERSION}/restic_${RESTIC_VERSION}_linux_amd64.bz2" \
 && echo "${RESTIC_SHA256}  /restic.bz2" | sha256sum -c - \
 && bunzip2 /restic.bz2 && chmod 0755 /restic

FROM alpine:${ALPINE_VERSION}
# mariadb-connector-c ships the caching_sha2_password client plugin (MySQL 8 on DO). postgresql17-client matches the
# managed PostgreSQL major (infra/variables.tf postgres_version); bump both together.
RUN apk add --no-cache mariadb-client mariadb-connector-c postgresql17-client jq ca-certificates \
 && adduser -D -u 10001 -h /tmp backup
COPY --from=restic /restic /usr/local/bin/restic
COPY picopaas-backup.sh /usr/local/bin/picopaas-backup
RUN chmod 0755 /usr/local/bin/picopaas-backup
USER 10001:10001
ENV HOME=/tmp RESTIC_CACHE_DIR=/tmp/restic-cache GOMAXPROCS=1 GOMEMLIMIT=256MiB
ENTRYPOINT ["/usr/local/bin/picopaas-backup"]
