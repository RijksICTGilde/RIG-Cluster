# Unified backup image for RIG Cluster
# Supports: PVC backups (kopia), PostgreSQL backups (pg_dump/pg_restore), MinIO backups (mc)
# Target image: ghcr.io/minbzk/base-images/rig-backup:latest

# mc uit onze eigen spiegel: MinIO distribueert hem niet meer. Waarom die spiegel er is
# staat in images/mc/Dockerfile, hoe je hem vult in images/mc/README.md.
ARG MC_VERSION=RELEASE.2025-08-13T08-35-41Z
FROM ghcr.io/minbzk/base-images/mc:${MC_VERSION} AS mc

FROM python:3.14-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Europe/Amsterdam \
    DEBIAN_FRONTEND=noninteractive

# Install system dependencies. upgrade pulls Debian security updates to keep the container-scan gate green.
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    age \
    jq \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Install Kopia - for encrypted backup/restore operations
# Architectuur: was hardgecodeerd op "x64" terwijl deze image voor amd64 en arm64 gebouwd
# wordt, dus arm64 kreeg een amd64-binary. Nu per TARGETARCH.
# renovate: datasource=github-releases depName=kopia/kopia extractVersion=^v(?<version>.+)$
ARG KOPIA_VERSION=0.23.1
ARG TARGETARCH
ARG KOPIA_SHA256_AMD64=416d0f84a3dbb321a8b2d8f0997b1a0a6e915babe79ee76fa6e4d2bd1e1c5178
ARG KOPIA_SHA256_ARM64=a4ffbc019e0b0f932e2632054e73ec521dc1e80172a00095369c53ecf4e5a6cb
RUN case "${TARGETARCH}" in \
      amd64) KOPIA_ARCH=x64; KOPIA_SHA256="${KOPIA_SHA256_AMD64}" ;; \
      arm64) KOPIA_ARCH=arm64; KOPIA_SHA256="${KOPIA_SHA256_ARM64}" ;; \
      *) echo "geen kopia-checksum voor TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl -fsSLO "https://github.com/kopia/kopia/releases/download/v${KOPIA_VERSION}/kopia-${KOPIA_VERSION}-linux-${KOPIA_ARCH}.tar.gz" \
    && echo "${KOPIA_SHA256}  kopia-${KOPIA_VERSION}-linux-${KOPIA_ARCH}.tar.gz" | sha256sum -c - \
    && tar -xzf "kopia-${KOPIA_VERSION}-linux-${KOPIA_ARCH}.tar.gz" \
    && mv "kopia-${KOPIA_VERSION}-linux-${KOPIA_ARCH}/kopia" /usr/local/bin/kopia \
    && chmod +x /usr/local/bin/kopia \
    && rm -rf "kopia-${KOPIA_VERSION}-linux-${KOPIA_ARCH}.tar.gz" "kopia-${KOPIA_VERSION}-linux-${KOPIA_ARCH}"

# Install MinIO client - for bucket operations
COPY --from=mc --chmod=755 /usr/bin/mc /usr/local/bin/mc

# Create non-root user with home directory for Kopia cache
RUN groupadd -g 1001 backupuser 2>/dev/null || true && \
    useradd -r -u 1001 -g 1001 -m -d /home/backupuser backupuser 2>/dev/null || \
    useradd -r -u 1001 -m -d /home/backupuser backupuser

# Create working directory and Kopia cache directories
WORKDIR /backup
RUN mkdir -p /home/backupuser/.cache/kopia && \
    chown -R backupuser:backupuser /backup /home/backupuser

# Switch to non-root user
USER backupuser

# Default command - can be overridden by pod spec
CMD ["echo", "RIG Backup Image - specify command in pod spec"]
