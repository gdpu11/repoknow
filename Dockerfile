FROM python:3.13-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
COPY app ./app
COPY LICENSE THIRD_PARTY_NOTICES.md ./
LABEL org.opencontainers.image.licenses="MIT"
RUN useradd --system --uid 10001 repoknow && mkdir /data && chown repoknow /data
USER repoknow
ENV REPOKNOW_DATA_DIR=/data
EXPOSE 8090
CMD ["python", "-m", "uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8090", "--workers", "1"]
