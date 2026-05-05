FROM python:3.11-slim

ENV POETRY_VERSION=2.1.4 \
    POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_CREATE=false \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN pip install --no-cache-dir "poetry==$POETRY_VERSION"

COPY pyproject.toml poetry.lock README.md /app/
RUN poetry install --only api

RUN pip install --no-cache-dir \
    --index-url https://download.pytorch.org/whl/cpu \
    torch==2.9.0

RUN pip install --no-cache-dir \
    sentence-transformers==5.4.1 \
    transformers==5.0.0 \
    tokenizers==0.22.1 \
    accelerate==1.11.0 \
    psycopg[binary]==3.2.4

COPY app /app/app
COPY .env.example /app/.env.example

RUN mkdir -p /app/artifacts

CMD ["python", "-m", "uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
