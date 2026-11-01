# Memos on PicoPaaS: the official image with one change. Upstream starts as root only to chown its data
# dir and then drops to its own `nonroot` user (UID 10001) with su-exec; PicoPaaS rejects root images.
# The image already owns /var/opt/memos as 10001, and its entrypoint skips the chown when not root.
FROM neosmemo/memos:26.10.0
USER 10001:10001
EXPOSE 5230
