from __future__ import annotations

import os
import uuid
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
    out = {
        attr.key: getattr(instance, attr.key)
        for attr in sa_inspect(instance).mapper.column_attrs
    }
    for key, value in out.items():
        if isinstance(value, uuid.UUID):
            out[key] = str(value)
    return out


def _migrate_integer_ids_to_uuid(conn) -> None:
    """One-time migration: serial integer PKs/FKs -> UUID (PostgreSQL)."""
    data_type = conn.execute(
        text(
            """
            SELECT data_type FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'admins' AND column_name = 'id'
            """
        )
    ).scalar()
    if data_type != "integer":
        return

    conn.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))

    for table in ("admins", "vehicles", "cameras", "parking_logs", "software_logs"):
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS id_new UUID"))
        conn.execute(text(f"UPDATE {table} SET id_new = gen_random_uuid() WHERE id_new IS NULL"))

    conn.execute(text("ALTER TABLE software_logs ADD COLUMN IF NOT EXISTS admin_id_new UUID"))
    conn.execute(
        text(
            """
            UPDATE software_logs sl
            SET admin_id_new = a.id_new
            FROM admins a
            WHERE sl.admin_id IS NOT NULL AND sl.admin_id = a.id
            """
        )
    )
    conn.execute(text("ALTER TABLE parking_logs ADD COLUMN IF NOT EXISTS vehicle_id_new UUID"))
    conn.execute(
        text(
            """
            UPDATE parking_logs pl
            SET vehicle_id_new = v.id_new
            FROM vehicles v
            WHERE pl.vehicle_id IS NOT NULL AND pl.vehicle_id = v.id
            """
        )
    )

    conn.execute(
        text(
            """
            DO $$ DECLARE r record;
            BEGIN
                FOR r IN (
                    SELECT conrelid::regclass::text AS tbl, conname
                    FROM pg_constraint
                    WHERE contype = 'f'
                      AND connamespace = current_schema()::regnamespace
                      AND conrelid::regclass::text IN ('software_logs', 'parking_logs')
                ) LOOP
                    EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I', r.tbl, r.conname);
                END LOOP;
            END $$;
            """
        )
    )

    for table in ("admins", "vehicles", "cameras", "parking_logs", "software_logs"):
        conn.execute(
            text(
                f"""
                DO $$ DECLARE cname text;
                BEGIN
                    SELECT conname INTO cname
                    FROM pg_constraint
                    WHERE contype = 'p'
                      AND conrelid = '{table}'::regclass
                      AND connamespace = current_schema()::regnamespace
                    LIMIT 1;
                    IF cname IS NOT NULL THEN
                        EXECUTE format('ALTER TABLE {table} DROP CONSTRAINT %I', cname);
                    END IF;
                END $$;
                """
            )
        )
        conn.execute(text(f"ALTER TABLE {table} DROP COLUMN id"))
        conn.execute(text(f"ALTER TABLE {table} RENAME COLUMN id_new TO id"))
        conn.execute(text(f"ALTER TABLE {table} ADD PRIMARY KEY (id)"))

    conn.execute(text("ALTER TABLE software_logs DROP COLUMN IF EXISTS admin_id"))
    conn.execute(text("ALTER TABLE software_logs RENAME COLUMN admin_id_new TO admin_id"))
    conn.execute(text("ALTER TABLE parking_logs DROP COLUMN IF EXISTS vehicle_id"))
    conn.execute(text("ALTER TABLE parking_logs RENAME COLUMN vehicle_id_new TO vehicle_id"))

    conn.execute(
        text(
            """
            ALTER TABLE software_logs
            ADD CONSTRAINT software_logs_admin_id_fkey
            FOREIGN KEY (admin_id) REFERENCES admins(id) ON DELETE SET NULL
            """
        )
    )
    conn.execute(
        text(
            """
            ALTER TABLE parking_logs
            ADD CONSTRAINT parking_logs_vehicle_id_fkey
            FOREIGN KEY (vehicle_id) REFERENCES vehicles(id) ON DELETE SET NULL
            """
        )
    )

    conn.execute(
        text(
            "ALTER TABLE software_logs ADD COLUMN IF NOT EXISTS admin_username VARCHAR(255)"
        )
    )


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


def _migrate_split_vehicles_plates(conn) -> None:
    """Move plate fields off vehicles into plates + plate_assignments."""
    has_plate_col = conn.execute(
        text(
            """
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'vehicles'
              AND column_name = 'plate_normalized'
            """
        )
    ).scalar()
    if not has_plate_col:
        return

    conn.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
    conn.execute(
        text(
            """
            INSERT INTO plates (
                id, plate_number, plate_normalized, plate_color,
                is_guest, guest_expires_at, deleted_at, created_at
            )
            SELECT
                gen_random_uuid(),
                v.plate_number,
                v.plate_normalized,
                COALESCE(v.plate_color, 'default'),
                COALESCE(v.is_guest, false),
                v.guest_expires_at,
                v.deleted_at,
                COALESCE(v.created_at, now())
            FROM vehicles v
            WHERE v.plate_normalized IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM plates p WHERE p.plate_normalized = v.plate_normalized
              )
            """
        )
    )
    conn.execute(
        text(
            """
            INSERT INTO plate_assignments (id, plate_id, vehicle_id, is_primary, created_at)
            SELECT gen_random_uuid(), p.id, v.id, true, COALESCE(v.created_at, now())
            FROM vehicles v
            JOIN plates p ON p.plate_normalized = v.plate_normalized
            WHERE NOT EXISTS (
                SELECT 1 FROM plate_assignments pa
                WHERE pa.plate_id = p.id AND pa.vehicle_id = v.id
            )
            """
        )
    )
    conn.execute(
        text("ALTER TABLE parking_logs ADD COLUMN IF NOT EXISTS plate_id UUID")
    )
    conn.execute(
        text(
            """
            UPDATE parking_logs pl
            SET plate_id = p.id
            FROM plates p
            WHERE pl.plate_id IS NULL
              AND pl.plate_normalized = p.plate_normalized
            """
        )
    )
    conn.execute(text("ALTER TABLE vehicles DROP COLUMN IF EXISTS plate_number"))
    conn.execute(text("ALTER TABLE vehicles DROP COLUMN IF EXISTS plate_normalized"))
    conn.execute(text("ALTER TABLE vehicles DROP COLUMN IF EXISTS plate_color"))
    conn.execute(text("ALTER TABLE vehicles DROP COLUMN IF EXISTS is_guest"))
    conn.execute(text("ALTER TABLE vehicles DROP COLUMN IF EXISTS guest_expires_at"))
    conn.execute(
        text(
            """
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'parking_logs_plate_id_fkey'
                ) THEN
                    ALTER TABLE parking_logs
                    ADD CONSTRAINT parking_logs_plate_id_fkey
                    FOREIGN KEY (plate_id) REFERENCES plates(id) ON DELETE SET NULL;
                END IF;
            END $$;
            """
        )
    )


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
        _migrate_integer_ids_to_uuid(conn)
        _migrate_split_vehicles_plates(conn)
        conn.execute(
            text(
                "ALTER TABLE software_logs ADD COLUMN IF NOT EXISTS admin_username VARCHAR(255)"
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
