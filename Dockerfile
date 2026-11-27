# syntax=docker/dockerfile:1@sha256:572cebbc2b15e6f84c077742952674889cb63cd5e9b80abdffcbe587b6f19183
FROM python:3.15-slim-bookworm@sha256:498829c717b1e59fc42f0e7865eafe4e04ca0f70a3769d4ae70ba218df54507c

# App version, stamped from the release tag by CI (docker-publish.yml build-args).
ARG VERSION=0.0.0-dev
ENV APP_VERSION=${VERSION} \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TOYS_DATA=/data \
    HOME=/tmp

LABEL org.opencontainers.image.source="https://github.com/LukeEvansTech/toys" \
      org.opencontainers.image.title="toys" \
      org.opencontainers.image.description="Statue and garage-kit collection, and a watch for new listings" \
      org.opencontainers.image.version="${VERSION}"

WORKDIR /app
COPY requirements.txt ./
# git: pip installs collectorkit from its tag. curl: the feed collectors use it when present,
# because some shops refuse Python's TLS stack. Not version-pinned: the base image digest fixes
# the Debian release, and a pinned point version vanishes from the mirror after a security update.
# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends curl git \
    && pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y git && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# The shops without a product feed are rendered by obscura. Pinned by version and checksum;
# the release is x86_64, as the cluster is.
ARG OBSCURA_VERSION=0.2.7
ARG OBSCURA_SHA256=11b53d27cd743e00e196e216920e7f3f794cf378e3f1792b8d25d3beabaf9cad
RUN curl -fsSL -o /tmp/obscura.tgz \
      "https://github.com/h4ckf0r0day/obscura/releases/download/v${OBSCURA_VERSION}/obscura-x86_64-linux.tar.gz" \
    && echo "${OBSCURA_SHA256}  /tmp/obscura.tgz" | sha256sum -c - \
    && tar -xzf /tmp/obscura.tgz -C /usr/local/bin obscura obscura-worker \
    && rm /tmp/obscura.tgz

COPY toys ./toys

RUN mkdir -p /data && chown -R 65532:65532 /data
VOLUME ["/data"]

USER 65532:65532
# Kubernetes probes own liveness (/health); an image healthcheck would double-probe.
HEALTHCHECK NONE
EXPOSE 8080
ENTRYPOINT ["python", "-m", "toys"]
CMD ["serve"]
