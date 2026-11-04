# CostLens synthetic demo. Standard library only, so nothing is installed.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN useradd --uid 10001 --create-home costlens \
    && mkdir /data \
    && chown costlens:costlens /data

WORKDIR /app
COPY costlens ./costlens

USER costlens
VOLUME /data
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=4)"]

# Refuses to start unless COSTLENS_BEHIND_AUTH_PROXY and COSTLENS_ALLOWED_HOSTS are set.
CMD ["python", "-m", "costlens", "demo", "--host", "0.0.0.0", "--data-dir", "/data"]
