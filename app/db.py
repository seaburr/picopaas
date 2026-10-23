from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import DATABASE_URL, DB_MAX_OVERFLOW, DB_POOL_RECYCLE, DB_POOL_SIZE, DB_POOL_TIMEOUT
from app.db_tls import engine_url_and_args

_sqlite = DATABASE_URL.startswith("sqlite")
_url, _connect_args = engine_url_and_args(DATABASE_URL)

_pool_kwargs = {} if _sqlite else {
    "pool_size": DB_POOL_SIZE,
    "max_overflow": DB_MAX_OVERFLOW,
    "pool_timeout": DB_POOL_TIMEOUT,
    "pool_recycle": DB_POOL_RECYCLE,
    "pool_pre_ping": True,
}

engine = create_engine(_url, connect_args=_connect_args, **_pool_kwargs)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
