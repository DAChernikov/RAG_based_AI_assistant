from sqlalchemy import select

from app.api.config import settings
from app.state.database import create_database_engine, create_session_factory
from app.state.models import Tenant, User


def seed_development_identity() -> tuple[str, str]:
    engine = create_database_engine()
    session_factory = create_session_factory(engine)
    with session_factory.begin() as session:
        tenant = session.scalar(
            select(Tenant).where(Tenant.slug == settings.compatibility_tenant_slug)
        )
        if tenant is None:
            tenant = Tenant(slug=settings.compatibility_tenant_slug, name="Development")
            session.add(tenant)
            session.flush()
        user = session.scalar(
            select(User).where(
                User.tenant_id == tenant.id,
                User.external_id == settings.compatibility_user_external_id,
            )
        )
        if user is None:
            user = User(
                tenant_id=tenant.id,
                external_id=settings.compatibility_user_external_id,
                display_name="Development User",
            )
            session.add(user)
            session.flush()
        return str(tenant.id), str(user.id)


def main() -> None:
    seed_development_identity()
    print("Development compatibility identity is ready.")


if __name__ == "__main__":
    main()
