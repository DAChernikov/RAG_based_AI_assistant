import asyncio
import uuid

from telegram.ext import Application, CommandHandler, MessageHandler, filters

from app.bot.api_client import APIClient
from app.bot.config import bot_settings
from app.bot.handlers import ready_handler, start_handler, text_handler
from app.concurrency import connector_blocking_io
from app.observability import log_event


def build_application(token: str, api_key: str | None = None) -> Application:
    if not token:
        raise RuntimeError("A Telegram bot token is required to start polling.")
    application = Application.builder().token(token).build()
    if api_key:
        application.bot_data["api_client"] = APIClient(api_key=api_key)

    application.add_handler(CommandHandler("start", start_handler))
    application.add_handler(CommandHandler("ready", ready_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    return application


async def _start(application: Application) -> None:
    await application.initialize()
    await application.start()
    await application.updater.start_polling()


async def _stop(application: Application | None) -> None:
    if application is not None:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()


async def run_bot() -> None:
    """Run idle until UI configuration is enabled; reload on config version changes."""
    from redis.asyncio import Redis

    from app.api.config import settings
    from app.operations.repository import OperationsRepository
    from app.secrets.store import EncryptedDatabaseSecretStore, SecretStoreError, load_master_key
    from app.state.database import create_database_engine, create_session_factory
    from app.worker_healthcheck import record_local_heartbeat

    engine = create_database_engine()
    repository = OperationsRepository(create_session_factory(engine))
    secret_store = EncryptedDatabaseSecretStore(
        repository.session_factory, load_master_key(settings)
    )
    applications: dict[tuple[uuid.UUID, uuid.UUID], tuple[int, Application]] = {}
    heartbeat_redis = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        while True:
            record_local_heartbeat("bot")
            await heartbeat_redis.set(
                "rag:bot:heartbeat", "idle" if not applications else "active", ex=20
            )
            rows = await connector_blocking_io.call(repository.list_enabled_telegram_configurations)
            desired = {(row.tenant_id, getattr(row, "id", row.tenant_id)): row for row in rows}
            for key, (_version, application) in list(applications.items()):
                row = desired.get(key)
                if row is None or row.config_version != applications[key][0]:
                    await _stop(application)
                    applications.pop(key)
            for key, row in desired.items():
                if key in applications:
                    continue
                tenant_id = row.tenant_id
                if row.token_credential_ref and row.api_key_credential_ref:
                    try:
                        token = await connector_blocking_io.call(
                            secret_store.resolve, tenant_id, row.token_credential_ref
                        )
                        api_key = await connector_blocking_io.call(
                            secret_store.resolve, tenant_id, row.api_key_credential_ref
                        )
                        application = build_application(token["value"], api_key["value"])
                        await _start(application)
                        applications[key] = (row.config_version, application)
                    except (SecretStoreError, KeyError, RuntimeError) as exc:
                        log_event(
                            "telegram_configuration_failed",
                            tenant_id=str(tenant_id),
                            error_type=type(exc).__name__,
                        )
                        continue
                    except Exception as exc:
                        log_event(
                            "telegram_start_failed",
                            tenant_id=str(tenant_id),
                            error_type=type(exc).__name__,
                        )
                        continue
            await asyncio.sleep(bot_settings.bot_config_reload_interval_sec)
    finally:
        for _version, application in applications.values():
            await _stop(application)
        await heartbeat_redis.aclose()
        engine.dispose()


def main() -> None:
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
