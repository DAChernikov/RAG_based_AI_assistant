FROM python:3.11.11-slim-bookworm@sha256:081075da77b2b55c23c088251026fb69a7b2bf92471e491ff5fd75c192fd38e5 AS builder
ARG POETRY_VERSION=2.1.4
ENV POETRY_NO_INTERACTION=1 POETRY_VIRTUALENVS_IN_PROJECT=true
WORKDIR /app
RUN pip install --no-cache-dir "poetry==$POETRY_VERSION"
COPY pyproject.toml poetry.lock README.md ./
RUN poetry install --only api,embedding --no-root

FROM python:3.11.11-slim-bookworm@sha256:081075da77b2b55c23c088251026fb69a7b2bf92471e491ff5fd75c192fd38e5 AS runtime
ENV PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH"
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY app ./app
RUN useradd --no-create-home --uid 10004 embedding && mkdir -p /models && chown 10004:10004 /models
USER 10004
EXPOSE 8001
CMD ["python", "-m", "uvicorn", "app.embedding_service.main:app", "--host", "0.0.0.0", "--port", "8001"]
