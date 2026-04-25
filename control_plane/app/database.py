from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from .config import settings

# connect_args differ between PostgreSQL and SQLite
_is_sqlite = settings.DATABASE_URL.startswith("sqlite")
_connect_args = {} if _is_sqlite else {"connect_timeout": 10}
_pool_kwargs  = {} if _is_sqlite else {"pool_size": 20, "max_overflow": 40, "pool_recycle": 3600}

engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,
    connect_args=_connect_args,
    **_pool_kwargs,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """FastAPI dependency – yields a DB session and closes it after the request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
