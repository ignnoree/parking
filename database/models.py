"""SQLAlchemy ORM models — parking ANPR."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Admin(Base):
    __tablename__ = "admins"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(255), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    refresh_jti: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role: Mapped[str] = mapped_column(String(32), server_default="worker", default="worker")


class Vehicle(Base):
    __tablename__ = "vehicles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plate_number: Mapped[str] = mapped_column(String(32))
    plate_normalized: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    owner_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    owner_lastname: Mapped[str | None] = mapped_column(String(255), nullable=True)
    car_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    door_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    floor_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parking_spot: Mapped[str | None] = mapped_column(String(64), nullable=True)
    plate_color: Mapped[str] = mapped_column(String(32), server_default="default", default="default")
    vehicle_class: Mapped[str] = mapped_column(String(32), server_default="car", default="car")
    is_guest: Mapped[bool] = mapped_column(Boolean, default=False)
    guest_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reference_image_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ParkingLog(Base):
    __tablename__ = "parking_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vehicle_id: Mapped[int | None] = mapped_column(
        ForeignKey("vehicles.id", ondelete="SET NULL"), nullable=True
    )
    plate_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    plate_normalized: Mapped[str] = mapped_column(String(32), index=True)
    direction: Mapped[str] = mapped_column(String(16))
    match_status: Mapped[str] = mapped_column(String(32))
    is_guest: Mapped[bool] = mapped_column(Boolean, default=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    snapshot_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source: Mapped[str] = mapped_column(String(64), default="plate_recognition")
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    logged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SoftwareLog(Base):
    __tablename__ = "software_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    level: Mapped[str] = mapped_column(String(20))
    event: Mapped[str | None] = mapped_column(String(255), nullable=True)
    module: Mapped[str | None] = mapped_column(String(255), nullable=True)
    message: Mapped[str] = mapped_column(Text)
    metadata_: Mapped[str | None] = mapped_column("metadata", Text, nullable=True)
    logged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Camera(Base):
    __tablename__ = "cameras"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128))
    protocol: Mapped[str] = mapped_column(String(16))
    source: Mapped[str] = mapped_column(String(512))
    gate_role: Mapped[str] = mapped_column(String(16), server_default="entry", default="entry")
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    light_profile: Mapped[str] = mapped_column(String(32), server_default="normal", default="normal")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
