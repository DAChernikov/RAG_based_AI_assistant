install:
	poetry install --with bot,dev

run-api:
	poetry run python -m uvicorn app.api.main:app --host 127.0.0.1 --port 8000

run-api-reload:
	poetry run python -m uvicorn app.api.main:app --reload --host 127.0.0.1 --port 8000

run-bot:
	poetry run python -m app.bot.main

test:
	poetry run pytest

fmt:
	poetry run black app tests
	poetry run isort app tests

lint:
	poetry run ruff check app tests

up:
	docker compose up --build

down:
	docker compose down