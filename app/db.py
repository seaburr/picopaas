from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import DATABASE_URL
from app.db_tls import engine_url_and_args

_sqlite = DATABASE_URL.startswith("sqlite")
_url, _connect_args = engine_url_and_args(DATABASE_URL)

engine = create_engine(
    _url,
    connect_args=_connect_args,
    pool_pre_ping=not _sqlite,
    pool_recycle=1800 if not _sqlite else -1,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
