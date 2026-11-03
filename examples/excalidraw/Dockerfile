# Excalidraw on PicoPaaS: the upstream image is nginx as root on port 80. This copies its built static site into
# nginx-unprivileged (port 8080; works as UID 10001, see README).
FROM excalidraw/excalidraw:latest AS site

FROM nginxinc/nginx-unprivileged:stable-alpine
COPY --from=site /usr/share/nginx/html /usr/share/nginx/html
COPY default.conf /etc/nginx/conf.d/default.conf
EXPOSE 8080
