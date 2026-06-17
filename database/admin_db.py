from werkzeug.security import generate_password_hash
from sqlalchemy import func, select
from uuid import UUID

from database.db import session_scope, instance_to_dict
from database.models import Admin
from helpers.uuid_utils import parse_uuid

ROLE_SYSTEM_ADMIN = "system_admin"
ROLE_PARKING_ADMIN = "parking_admin"
ROLE_WORKER = "worker"
VALID_ROLES = (ROLE_SYSTEM_ADMIN, ROLE_PARKING_ADMIN, ROLE_WORKER)


def init_default_admin() -> None:
    with session_scope() as session:
        exists = session.scalar(select(Admin.id).where(Admin.username == "admin"))
        if exists is None:
            session.add(
                Admin(
                    username="admin",
                    password_hash=generate_password_hash("1234"),
                    role=ROLE_SYSTEM_ADMIN,
                )
            )


def get_admin_by_username(username: str) -> dict | None:
    with session_scope() as session:
        admin = session.scalar(select(Admin).where(Admin.username == username))
        return instance_to_dict(admin) if admin else None


def get_admin_by_id(admin_id: UUID | str) -> dict | None:
    uid = parse_uuid(admin_id)
    if uid is None:
        return None
    with session_scope() as session:
        admin = session.get(Admin, uid)
        return instance_to_dict(admin) if admin else None


def update_admin_refresh_jti(admin_id: UUID | str, jti: str | None) -> None:
    uid = parse_uuid(admin_id)
    if uid is None:
        return
    with session_scope() as session:
        admin = session.get(Admin, uid)
        if admin:
            admin.refresh_jti = jti


def list_admins(*, roles: list[str] | None = None) -> list[dict]:
    with session_scope() as session:
        stmt = select(Admin).order_by(Admin.id.asc())
        if roles is not None:
            stmt = stmt.where(Admin.role.in_(roles))
        rows = session.execute(stmt).scalars().all()
        out: list[dict] = []
        for row in rows:
            d = instance_to_dict(row)
            d.pop("password_hash", None)
            d.pop("refresh_jti", None)
            out.append(d)
        return out


def insert_admin(username: str, password_plain: str, role: str) -> UUID | None:
    if role not in VALID_ROLES:
        return None
    with session_scope() as session:
        if session.scalar(select(Admin.id).where(Admin.username == username)):
            return None
        admin = Admin(
            username=username,
            password_hash=generate_password_hash(password_plain),
            role=role,
        )
        session.add(admin)
        session.flush()
        return admin.id


def update_admin(
    admin_id: UUID | str,
    *,
    role: str | None = None,
    password_plain: str | None = None,
) -> bool:
    if role is not None and role not in VALID_ROLES:
        return False
    uid = parse_uuid(admin_id)
    if uid is None:
        return False
    with session_scope() as session:
        admin = session.get(Admin, uid)
        if admin is None:
            return False
        if role is not None:
            admin.role = role
        if password_plain:
            admin.password_hash = generate_password_hash(password_plain)
        return True


def delete_admin_by_id(admin_id: UUID | str) -> bool:
    uid = parse_uuid(admin_id)
    if uid is None:
        return False
    with session_scope() as session:
        admin = session.get(Admin, uid)
        if admin is None:
            return False
        session.delete(admin)
        return True
