#!/usr/bin/env bash
# CI smoke for the Kaisen runtime image: serve a tiny static bundle from this host, run boot.py in the image as
# UID 10001 and expect HTTP 200 on the app port. (Adapted from smoke-local.sh, which needs a real project bundle.)
#   images/kaisen-runtime/ci-smoke.sh <image>        needs Docker + python3 + curl (Linux runner: --network host)
set -euo pipefail
image=${1:?usage: $0 image}
serve_port=${SERVE_PORT:-8799}
work=$(mktemp -d)
trap 'docker rm -f kaisen-smoke >/dev/null 2>&1 || true; kill "${srv:-0}" 2>/dev/null || true; rm -rf "$work"' EXIT

mkdir "$work/site"
echo '<!doctype html><title>kaisen smoke</title><p>ok</p>' > "$work/site/index.html"
# a static project with a node test: boot.py must run it before serving and print a passing result
mkdir "$work/site/tests"
echo '{"runtime":"static","test":"node --test"}' > "$work/site/kaisen.json"
printf "const t=require('node:test');const a=require('node:assert');t('ok',()=>a.equal(1,1));\n" > "$work/site/tests/ok.test.js"
tar -czf "$work/serve-b.tar.gz" -C "$work/site" index.html kaisen.json tests
mkdir "$work/serve" && mv "$work/serve-b.tar.gz" "$work/serve/b.tar.gz"
sha=$(sha256sum "$work/serve/b.tar.gz" | cut -d' ' -f1)
(cd "$work/serve" && exec python3 -m http.server "$serve_port" --bind 127.0.0.1 >/dev/null 2>&1) & srv=$!
sleep 1

docker run -d --name kaisen-smoke --network host --user 10001 \
  -e KAISEN_BUNDLE_URL="http://127.0.0.1:$serve_port/b.tar.gz" -e KAISEN_BUNDLE_SHA256="$sha" "$image" >/dev/null

code=000
for _ in $(seq 1 60); do
  code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:8080/" || true)
  [ "$code" = 200 ] && break
  docker ps -q -f name=kaisen-smoke | grep -q . || break # container exited
  sleep 1
done
docker logs kaisen-smoke 2>&1 | tail -20
echo "HTTP $code"
[ "$code" = 200 ]
docker logs kaisen-smoke 2>&1 | grep -q '^KAISEN_TESTS_RESULT pass exit=0' || { echo "static test result missing"; exit 1; }
