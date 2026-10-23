# syntax=docker/dockerfile:1.4
FROM docker.m.daocloud.io/library/python:3.12-slim

RUN sed -i 's|http://deb.debian.org|http://mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg git openssh-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/viknow3

COPY pyproject.toml README.md requirements-hyperrag.txt ./
COPY src ./src
COPY external/HyperRAG ./external/HyperRAG
COPY external/schedule-core ./external/schedule-core

# HyperRAG core only (no torch/mineru). Indexing: install external/HyperRAG[index] in a writer image.
RUN pip install --no-cache-dir \
    -i https://mirrors.aliyun.com/pypi/simple/ \
    --trusted-host mirrors.aliyun.com \
    ./external/schedule-core \
    -e . \
    -r requirements-hyperrag.txt

ENV PYTHONPATH=/opt/viknow3/src
ENV VIKNOW3_ROOT=/opt/viknow3
ENV VIKNOW_HYPERRAG_REPO_ROOT=/opt/viknow3/external/HyperRAG
ENV VIKNOW_STATE_DIR=/tmp/viknow3-state
ENV VIKNOW_HOST=0.0.0.0
ENV VIKNOW_PORT=5197

EXPOSE 5197
CMD ["python", "-m", "viknow3"]
