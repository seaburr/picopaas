# Vaultwarden on PicoPaaS: the official alpine image with one change. Upstream has no USER (root);
# PicoPaaS rejects root images. Nothing in /start.sh needs root, and /data is a volume the pod's
# fsGroup makes writable.
FROM vaultwarden/server:1.37.4-alpine
USER 10001:10001
# Rocket must listen above 1024.
ENV ROCKET_PORT=8080
EXPOSE 8080
