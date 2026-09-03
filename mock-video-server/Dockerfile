FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install --no-install-recommends -y iproute2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/mock-video-server
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py entrypoint.sh ./

RUN chmod 0755 /opt/mock-video-server/entrypoint.sh
EXPOSE 28500/tcp
ENTRYPOINT ["/opt/mock-video-server/entrypoint.sh"]
