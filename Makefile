PYTHON_PATHS = app tests migrations/env.py migrations/versions research/src

install:
	poetry install --with bot,worker,research,dev

run-api:
	poetry run python -m uvicorn app.api.main:app --host 127.0.0.1 --port 8000

run-api-reload:
	poetry run python -m uvicorn app.api.main:app --reload --host 127.0.0.1 --port 8000

run-bot:
	poetry run python -m app.bot.main

run-worker:
	poetry run python -m app.worker.main

run-ingestion-worker:
	poetry run python -m app.ingestion_worker.main

migrate:
	poetry run alembic upgrade head

migrate-down:
	poetry run alembic downgrade -1

seed-dev:
	poetry run python -m app.state.seed

bootstrap-admin:
	poetry run python -m app.state.bootstrap_admin \
		--tenant-slug "$(TENANT_SLUG)" \
		--tenant-name "$(TENANT_NAME)" \
		--username "$(ADMIN_USERNAME)" \
		--display-name "$(ADMIN_DISPLAY_NAME)"

test:
	poetry run pytest -m "not integration"

integration-test:
	INTEGRATION_DATABASE_URL=$${INTEGRATION_DATABASE_URL:-postgresql+psycopg://rag:rag-dev-only@127.0.0.1:5432/rag} \
	INTEGRATION_REDIS_URL=$${INTEGRATION_REDIS_URL:-redis://127.0.0.1:6379/15} \
	poetry run pytest -m integration

smoke-test:
	INTEGRATION_DATABASE_URL=$${INTEGRATION_DATABASE_URL:-postgresql+psycopg://rag:rag-dev-only@127.0.0.1:5432/rag} \
	INTEGRATION_REDIS_URL=$${INTEGRATION_REDIS_URL:-redis://127.0.0.1:6379/15} \
	poetry run pytest -m integration tests/integration/test_smoke.py

fmt:
	poetry run black $(PYTHON_PATHS)
	poetry run isort $(PYTHON_PATHS)

format-check:
	poetry run black --check $(PYTHON_PATHS)
	poetry run isort --check-only $(PYTHON_PATHS)

lint:
	poetry run ruff check $(PYTHON_PATHS)

check: format-check lint test

infra-up:
	docker compose up -d postgres redis

up:
	docker compose up --build

down:
	docker compose down
