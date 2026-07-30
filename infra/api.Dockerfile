FROM python:3.11-slim AS builder

ARG POETRY_VERSION=2.1.4
ENV POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_IN_PROJECT=true
WORKDIR /app
RUN pip install --no-cache-dir "poetry==$POETRY_VERSION"
COPY pyproject.toml poetry.lock README.md /app/
RUN poetry install --only api --no-root

FROM python:3.11-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY app /app/app
COPY alembic.ini /app/alembic.ini
COPY migrations /app/migrations
RUN useradd --create-home --uid 10001 appuser
USER appuser
CMD ["python", "-m", "uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
