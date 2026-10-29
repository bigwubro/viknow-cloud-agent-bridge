# syntax=docker/dockerfile:1.4
# Playground on 236: ViKnow API (5218) + UI (5198), HyperRAG index + deepagents.
FROM docker.m.daocloud.io/library/docker:27-cli AS dockercli
FROM docker.m.daocloud.io/library/python:3.12-slim

RUN sed -i 's|http://deb.debian.org|http://mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg git openssh-client curl bubblewrap \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd -g 1013 viknow || true \
    && useradd -u 1012 -g 1013 -m -s /bin/bash viknow || true \
    && groupadd -g 999 docker 2>/dev/null || true \
    && usermod -aG docker viknow || true

WORKDIR /opt/viknow-platform

COPY pyproject.toml README.md requirements-hyperrag.txt ./
COPY config ./config
COPY src ./src
COPY ui ./ui
COPY scripts/mineru-docker-wrapper scripts/mineru-docker-wrapper
COPY external/HyperRAG ./external/HyperRAG
COPY external/deepagents-sandbox ./external/deepagents-sandbox
COPY external/actio ./external/actio
COPY external/schedule-core ./external/schedule-core

RUN pip install --no-cache-dir \
    -i https://mirrors.aliyun.com/pypi/simple/ \
    --trusted-host mirrors.aliyun.com \
    ./external/deepagents-sandbox[langchain] \
    -e ./external/actio \
    ./external/schedule-core \
    -e . \
    -r requirements-hyperrag.txt

COPY --from=dockercli /usr/local/bin/docker /usr/bin/docker

RUN mkdir -p /opt/viknow-platform/bin \
    && ln -sf /opt/viknow-platform/scripts/mineru-docker-wrapper /opt/viknow-platform/bin/mineru

COPY deploy/236/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh /opt/viknow-platform/scripts/mineru-docker-wrapper

ENV PYTHONPATH=/opt/viknow-platform/src
ENV VIKNOW_PLATFORM_ROOT=/opt/viknow-platform
ENV VIKNOW_HYPERRAG_REPO_ROOT=/opt/viknow-platform/external/HyperRAG
ENV PATH=/opt/viknow-platform/bin:${PATH}

EXPOSE 5198 5218
USER viknow
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
