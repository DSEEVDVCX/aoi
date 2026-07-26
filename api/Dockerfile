FROM python:3.12-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir --prefix=/install .


FROM python:3.12-slim

WORKDIR /app

RUN useradd -r -m -u 1000 app

COPY --from=builder /install /usr/local
COPY --from=builder /build/src ./src

RUN playwright install chromium && playwright install-deps chromium && \
    chown -R app:app /app

USER app

EXPOSE 8000

CMD ["uvicorn", "fomo_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
