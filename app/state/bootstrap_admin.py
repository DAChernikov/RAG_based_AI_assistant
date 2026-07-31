from __future__ import annotations

import argparse
import getpass
import os

from sqlalchemy import select

from app.auth.security import PasswordManager
from app.state.database import create_database_engine, create_session_factory
from app.state.models import Tenant, User, UserRole


def bootstrap_admin(
    tenant_slug: str,
    tenant_name: str,
    username: str,
    display_name: str,
    password: str,
) -> tuple[str, str]:
    password_hash = PasswordManager().hash(password)
    engine = create_database_engine()
    factory = create_session_factory(engine)
    with factory.begin() as session:
        tenant = session.scalar(select(Tenant).where(Tenant.slug == tenant_slug).with_for_update())
        if tenant is None:
            tenant = Tenant(slug=tenant_slug, name=tenant_name)
            session.add(tenant)
            session.flush()
        user = session.scalar(
            select(User).where(
                User.tenant_id == tenant.id,
                User.username == username,
            )
        )
        if user is None:
            user = User(
                tenant_id=tenant.id,
                username=username,
                display_name=display_name,
                password_hash=password_hash,
                role=UserRole.ADMIN.value,
                is_active=True,
            )
            session.add(user)
            session.flush()
        elif user.role != UserRole.ADMIN.value:
            raise RuntimeError("Existing user is not an administrator.")
        else:
            raise RuntimeError("Administrator already exists; use the admin API to modify it.")
        return str(tenant.id), str(user.id)


def main() -> None:
    parser = argparse.ArgumentParser(description="Create the first local tenant administrator.")
    parser.add_argument("--tenant-slug", required=True)
    parser.add_argument("--tenant-name", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--display-name", required=True)
    args = parser.parse_args()
    password = os.getenv("BOOTSTRAP_ADMIN_PASSWORD") or getpass.getpass(
        "Initial administrator password: "
    )
    tenant_id, user_id = bootstrap_admin(
        args.tenant_slug,
        args.tenant_name,
        args.username,
        args.display_name,
        password,
    )
    print(f"Administrator created. tenant_id={tenant_id} user_id={user_id}")


if __name__ == "__main__":
    main()
