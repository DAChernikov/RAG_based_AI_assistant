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
RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates git openssh-client \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /app/.venv /app/.venv
COPY app /app/app
RUN useradd --create-home --uid 10002 ingestion
USER ingestion
CMD ["python", "-m", "app.ingestion_worker.main"]
