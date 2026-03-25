from fastapi import Request

from app.api.config import settings


class AppStateError(RuntimeError):
    pass


def get_settings():
    return settings


def get_runtime_state(request: Request) -> dict:
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:
        raise AppStateError("Application runtime state is not initialized.")
    return runtime
