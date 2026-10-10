# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=100 \
    PIP_RETRIES=10 \
    PIP_RESUME_RETRIES=20

WORKDIR /app

# Install dependencies first so code changes don't re-download them. The cache mount keeps
# pip's downloads between builds, so changing a dependency doesn't re-fetch the large
# language-ID wheel; PIP_RESUME_RETRIES resumes interrupted downloads on slow links.
COPY pyproject.toml ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade "pip>=25.1" \
    && mkdir -p src/collector && touch src/collector/__init__.py \
    && pip install ".[nlp]" \
    && pip uninstall -y ig-abuse-dataset

COPY src ./src
COPY alembic ./alembic
COPY alembic.ini ./
RUN pip install --no-cache-dir --no-deps .

EXPOSE 8000

CMD ["sh", "-c", "alembic upgrade head && uvicorn collector.api:app --host 0.0.0.0 --port 8000"]
