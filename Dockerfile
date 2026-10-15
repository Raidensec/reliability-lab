FROM python:3.12.14-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    RELIABILITY_HOST=0.0.0.0 \
    RELIABILITY_DB_PATH=/data/reliability.db

WORKDIR /app
COPY src/ /app/src/
RUN mkdir -p /data && chown 10001:10001 /data

USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=2)"]
CMD ["python", "-m", "reliability_lab.cli", "serve"]
