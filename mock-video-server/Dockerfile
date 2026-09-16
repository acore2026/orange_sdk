FROM python:3.12-slim

ARG MOCK_VERSION=0.2.0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install --no-install-recommends -y iproute2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/mock-video-server
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py smoke_client.py entrypoint.sh ./

RUN chmod 0755 /opt/mock-video-server/entrypoint.sh
LABEL org.opencontainers.image.title="Agent Compute Sandbox Mock" \
      org.opencontainers.image.description="N6 mock for Agent SDK media, recognition and control interfaces" \
      org.opencontainers.image.version="${MOCK_VERSION}"
EXPOSE 28500/tcp
HEALTHCHECK --interval=10s --timeout=3s --retries=6 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:28500/healthz', timeout=2)"]
ENTRYPOINT ["/opt/mock-video-server/entrypoint.sh"]
