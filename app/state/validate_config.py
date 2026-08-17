"""Fail-closed configuration preflight without logging configuration values."""

from pydantic import ValidationError

from app.api.config import Settings


def main() -> int:
    try:
        Settings()
    except ValidationError as exc:
        fields = sorted({".".join(map(str, error["loc"])) for error in exc.errors()})
        print(f"Configuration is invalid. Check: {', '.join(fields) or 'environment'}")
        return 1
    print("Configuration is valid for the selected APP_ENV.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
