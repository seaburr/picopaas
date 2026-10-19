#!/bin/sh
# Apply pending migrations, then run the app. Fails fast if either step fails
# (set -e), which matters for `alembic upgrade head` since a bad migration
# must not be masked by uvicorn starting anyway.
set -e

alembic upgrade head
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
