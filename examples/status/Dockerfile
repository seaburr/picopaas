# Status / uptime page on PicoPaaS: Gatus with your config baked in (apps have no config-file mount).
# The config contains only public URLs; pass secrets (e.g. webhook URLs) as app env vars, never bake them in.
FROM twinproduction/gatus:v5.37.0

# Upstream image is FROM scratch and runs as root by default; PicoPaaS rejects root images.
# A numeric USER works on scratch (no /etc/passwd needed) and matches the platform's forced UID 10001.
USER 10001:10001

COPY config.yaml /config/config.yaml
ENV GATUS_CONFIG_PATH=/config/config.yaml
# Gatus listens on 8080 by default (PicoPaaS ports must be 1024-65535).
EXPOSE 8080
