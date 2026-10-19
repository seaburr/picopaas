from fastapi import FastAPI

from app.db import Base, engine
from app.routers import apps, auth

# No Alembic auto-run here: migrations are applied explicitly (`alembic upgrade head`).
# create_all is a no-op against tables that already exist, which keeps the SQLite
# test suite (in-memory, no migrations run) working without duplicating schema logic.
Base.metadata.create_all(bind=engine)

app = FastAPI(title="PicoPaaS API")

app.include_router(auth.router)
app.include_router(apps.router)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
