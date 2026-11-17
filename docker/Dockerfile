FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY alembic.ini ./
COPY migrations ./migrations
RUN pip install ".[browser]" && scrapling install

RUN useradd --create-home --uid 10001 omnisearch \
    && mkdir -p /data/runtime /data/chrome /data/ego \
    && chown -R omnisearch:omnisearch /app /data
USER omnisearch

ENV OMNISEARCH_RUNTIME_DIR=/data/runtime \
    OMNISEARCH_CHROME_USER_DATA_DIR=/data/chrome \
    OMNISEARCH_EGO_SCRIPT_DIR=/data/ego

CMD ["omnisearch-api"]
