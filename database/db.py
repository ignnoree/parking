from __future__ import annotations

import os
from contextlib import contextmanager

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from sqlalchemy import create_engine, event, text
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


def _apply_schema_migrations() -> None:
    """One-off cleanups for settings/columns removed from the ORM."""
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE cameras DROP COLUMN IF EXISTS frame_interval_seconds"))
        conn.execute(text("DELETE FROM settings WHERE key = 'CAMERA_FRAME_INTERVAL_SECONDS'"))
        conn.execute(
            text(
                """
                DO $$
                BEGIN
                    IF EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_schema = current_schema()
                          AND table_name = 'cameras'
                          AND column_name = 'gate_role'
                    ) THEN
                        ALTER TABLE cameras RENAME COLUMN gate_role TO direction;
                    END IF;
                END $$;
                """
            )
        )


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    _apply_schema_migrations()


def reset_db() -> None:
    from database.admin_db import init_default_admin
    from database.cameras_db import bootstrap_cameras_from_env
    from database.settings_db import bootstrap_default_settings

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    init_default_admin()
    bootstrap_cameras_from_env()
    bootstrap_default_settings()


def bootstrap_db() -> None:
    init_db()
    from database.admin_db import init_default_admin
    from database.cameras_db import bootstrap_cameras_from_env
    from database.settings_db import bootstrap_default_settings

    init_default_admin()
    bootstrap_cameras_from_env()
    bootstrap_default_settings()
