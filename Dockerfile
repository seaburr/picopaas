# Multi-stage build: frontend (Vue/Vite) -> static assets, then the Python
# API that serves them. Mirrors the seaburr/tribal pattern.

# --- Stage 1: build the frontend ---
FROM node:22-slim AS frontend-build
WORKDIR /frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
# docs/*.md are imported with ?raw (the in-product /guide pages); vite.config.ts aliases @docs -> ../docs.
COPY docs/ /docs/
# vite.config.ts outDir is "../static" (relative to /frontend), so this lands
# the build at /static, picked up by `COPY --from=frontend-build /static` below.
RUN npm run build

# --- Stage 2: the API image ---
FROM python:3.13-slim

# Non-root by policy (docs/ARCHITECTURE.md decision: no root images). Fixed
# numeric uid/gid so Kubernetes runAsNonRoot / runAsUser checks can verify it
# without resolving a name inside the image.
RUN groupadd --gid 10001 app && \
    useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY alembic/ ./alembic/
# Served to admins by GET /admin/docs/admin-guide (app/routers/admin_docs.py).
COPY docs/ADMIN_GUIDE.md ./docs/ADMIN_GUIDE.md
COPY alembic.ini ./
COPY entrypoint.sh ./
COPY --from=frontend-build /static ./static

RUN chmod +x ./entrypoint.sh && chown -R 10001:10001 /app

# Production by default: ENVIRONMENT=dev is the only mode allowed to run
# without SECRET_KEY/FERNET_KEY (app/config.py) and must never be the image
# default.
ENV ENVIRONMENT=production \
    PYTHONUNBUFFERED=1

USER 10001:10001

EXPOSE 8000

ENTRYPOINT ["./entrypoint.sh"]
