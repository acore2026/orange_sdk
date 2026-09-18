FROM python:3.12-slim-bookworm

ARG MOCK_VERSION=0.3.0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install --no-install-recommends -y iproute2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/mock-video-server
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py smoke_client.py entrypoint.sh ./
COPY services ./services

RUN chmod 0755 /opt/mock-video-server/entrypoint.sh
LABEL org.opencontainers.image.title="Agent Compute Sandbox Mock" \
      org.opencontainers.image.description="N6 mock for Agent SDK media, recognition and control interfaces" \
      org.opencontainers.image.version="${MOCK_VERSION}"
EXPOSE 9004/tcp 28501/tcp 28502/tcp
HEALTHCHECK --interval=15s --timeout=5s --start-period=15s --retries=5 \
    CMD ["python", "-c", "import urllib.request\nfor url in ('http://127.0.0.1:28501/healthz','http://127.0.0.1:28502/healthz','http://127.0.0.1:9004/health','http://127.0.0.1:8011/health'):\n    urllib.request.urlopen(url, timeout=3)"]
ENTRYPOINT ["/opt/mock-video-server/entrypoint.sh"]
