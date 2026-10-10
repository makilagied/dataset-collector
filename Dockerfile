FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=100

WORKDIR /app

# Install dependencies first so code changes don't re-download them.
COPY pyproject.toml ./
RUN mkdir -p src/collector && touch src/collector/__init__.py \
    && pip install --retries 10 ".[nlp]" \
    && pip uninstall -y ig-abuse-dataset

COPY src ./src
COPY alembic ./alembic
COPY alembic.ini ./
RUN pip install --no-deps .

EXPOSE 8000

CMD ["sh", "-c", "alembic upgrade head && uvicorn collector.api:app --host 0.0.0.0 --port 8000"]
