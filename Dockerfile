FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

RUN apt-get update \
    && apt-get install --yes --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 v2c \
    && useradd --uid 10001 --gid v2c --create-home v2c \
    && mkdir -p /data/work /data/artifacts \
    && chown -R v2c:v2c /data

COPY requirements-runtime.lock ./
RUN python -m pip install --no-cache-dir -r requirements-runtime.lock
COPY pyproject.toml README.md alembic.ini ./
COPY src ./src
COPY migrations ./migrations
RUN python -m pip install --no-cache-dir --no-deps .

USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "v2c.api:app", "--host", "0.0.0.0", "--port", "8000"]
