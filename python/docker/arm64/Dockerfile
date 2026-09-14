# syntax=docker/dockerfile:1.7

ARG PYTHON_BASE_IMAGE=python:3.12-slim-bookworm
FROM ${PYTHON_BASE_IMAGE}

ARG TARGETOS
ARG TARGETARCH
ARG SDK_VERSION=unknown

RUN test "${TARGETOS}" = "linux" && test "${TARGETARCH}" = "arm64"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    XDG_STATE_HOME=/var/lib/agent-sdk \
    PATH=/usr/local/bin:${PATH}

RUN --mount=type=cache,id=agent-sdk-arm64-apt-archives,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,id=agent-sdk-arm64-apt-lists,target=/var/lib/apt/lists,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean \
    && apt-get -o Acquire::Retries=5 update \
    && apt-get -o Acquire::Retries=5 install --yes --no-install-recommends \
        ca-certificates \
        curl \
        iproute2 \
        iputils-ping \
        procps \
        tini

WORKDIR /opt/agent-sdk
COPY . /opt/agent-sdk

RUN python -m pip install --no-cache-dir /opt/agent-sdk \
    && python -c "import agent_sdk; print(agent_sdk.__version__)" \
    && python -c "import av; source=av.open('/opt/agent-sdk/examples/assets/video-offload-test.mp4'); frame=next(source.decode(video=0)); assert (frame.width, frame.height) == (1280, 720); source.close()" \
    && sh -n /opt/agent-sdk/docker/arm64/start-agent-a.sh \
    && sh -n /opt/agent-sdk/docker/arm64/start-agent-b.sh \
    && install -m 0755 /opt/agent-sdk/docker/arm64/start-agent-a.sh /usr/local/bin/start-agent-a.sh \
    && install -m 0755 /opt/agent-sdk/docker/arm64/start-agent-b.sh /usr/local/bin/start-agent-b.sh \
    && install -m 0755 /opt/agent-sdk/docker/arm64/image-help.sh /usr/local/bin/agent-sdk-image-help.sh \
    && mkdir -p /var/lib/agent-sdk /var/log/agent-sdk \
    && chmod 0700 /var/lib/agent-sdk

LABEL org.opencontainers.image.title="Agent Connect SDK ARM64 Runtime" \
      org.opencontainers.image.description="Linux ARM64 runtime for Agent Connect SDK roles A and B" \
      org.opencontainers.image.version="${SDK_VERSION}"

VOLUME ["/var/lib/agent-sdk", "/var/log/agent-sdk"]

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/usr/local/bin/agent-sdk-image-help.sh"]
