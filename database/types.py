"""Shared SQLAlchemy column types."""

from __future__ import annotations

import uuid

from sqlalchemy import BigInteger, ForeignKey, Integer
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import mapped_column

# Signed integer aliases (32-bit / 64-bit).
I32 = Integer
I64 = BigInteger


def uuid_pk(**kwargs):
    return mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        **kwargs,
    )


def uuid_fk(
    foreign_key: str,
    *,
    nullable: bool = False,
    index: bool = False,
    ondelete: str = "SET NULL",
    **kwargs,
):
    return mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(foreign_key, ondelete=ondelete),
        nullable=nullable,
        index=index,
        **kwargs,
    )
