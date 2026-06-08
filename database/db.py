from __future__ import annotations

import os
from contextlib import contextmanager

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.inspection import inspect as sa_inspect

from database.models import Base

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is required")

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False)


@event.listens_for(engine, "connect")
def _set_connection_timezone_utc(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("SET TIME ZONE 'UTC'")
    cursor.close()


def instance_to_dict(instance) -> dict:
    return {
        attr.key: getattr(instance, attr.key)
        for attr in sa_inspect(instance).mapper.column_attrs
    }


@contextmanager
def session_scope():
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db() -> None:
    Base.metadata.create_all(bind=engine)


def reset_db() -> None:
    from database.admin_db import init_default_admin

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    init_default_admin()


def bootstrap_db() -> None:
    init_db()
    from database.admin_db import init_default_admin

    init_default_admin()
