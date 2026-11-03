# ntfy on PicoPaaS: the official image with one change. Upstream sets no USER (it runs as root, which the
# platform's image check rejects: "Image runs as root"); the Go binary needs no root when it listens above 1024.
# With NTFY_DATABASE_URL set it writes no files, so nothing else needs to be writable.
FROM binwiederhier/ntfy:v2.37.1
USER 10001:10001
EXPOSE 8080
