# IT-Tools on PicoPaaS: the upstream image is nginx as root on port 80. This copies its built static site into
# nginx-unprivileged (UID 101 by default, port 8080); PicoPaaS forces UID 10001, which works because the
# unprivileged image keeps its cache/pid/temp paths world-writable (verified with --user 10001).
# Upstream's last image is 2024-10-22 (the project's repo is still active); pin the digest or build from source to refresh.
FROM corentinth/it-tools:latest AS site

FROM nginxinc/nginx-unprivileged:stable-alpine
COPY --from=site /usr/share/nginx/html /usr/share/nginx/html
# Single-page app: unknown paths fall back to index.html (same rule as upstream's nginx.conf).
COPY default.conf /etc/nginx/conf.d/default.conf
EXPOSE 8080
