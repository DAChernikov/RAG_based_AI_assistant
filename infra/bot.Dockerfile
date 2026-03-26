FROM python:3.11-slim

ENV POETRY_VERSION=2.1.4 \
    POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_CREATE=false \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN pip install --no-cache-dir "poetry==$POETRY_VERSION"

COPY pyproject.toml poetry.lock README.md /app/
RUN poetry install --only bot

COPY app /app/app
COPY .env.example /app/.env.example

CMD ["python", "-m", "app.bot.main"]