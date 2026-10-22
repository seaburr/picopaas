# PicoPaaS status page: Gatus with our config baked in (apps have no config-file mount).
# Public image (ghcr.io/seaburr/picopaas-status); contains only public URLs, no secrets.
FROM twinproduction/gatus:v5.37.0

# Upstream image is FROM scratch and runs as root by default; PicoPaaS rejects root images.
# A numeric USER works on scratch (no /etc/passwd needed) and matches app/config.py NAMED_USER_RUN_AS_UID.
USER 10001:10001

COPY config.yaml /config/config.yaml
ENV GATUS_CONFIG_PATH=/config/config.yaml
# Gatus listens on 8080 by default (PicoPaaS ports must be 1024-65535).
EXPOSE 8080
