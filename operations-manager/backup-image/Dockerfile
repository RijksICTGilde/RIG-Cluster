# Unified backup image for RIG Cluster
# Supports: PVC backups (kopia), PostgreSQL backups (pg_dump/pg_restore), MinIO backups (mc)
# Target image: ghcr.io/minbzk/base-images/rig-backup:latest

FROM python:3.14-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Europe/Amsterdam \
    DEBIAN_FRONTEND=noninteractive

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    age \
    jq \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Install Kopia - for encrypted backup/restore operations
ARG KOPIA_VERSION=0.23.1
RUN curl -LO "https://github.com/kopia/kopia/releases/download/v${KOPIA_VERSION}/kopia-${KOPIA_VERSION}-linux-x64.tar.gz" \
    && tar -xzf "kopia-${KOPIA_VERSION}-linux-x64.tar.gz" \
    && mv "kopia-${KOPIA_VERSION}-linux-x64/kopia" /usr/local/bin/kopia \
    && chmod +x /usr/local/bin/kopia \
    && rm -rf "kopia-${KOPIA_VERSION}-linux-x64.tar.gz" "kopia-${KOPIA_VERSION}-linux-x64"

# Install MinIO client - for bucket operations.
# Zelfde bron en zelfde grendel als in operations-manager/Dockerfile: MinIO distribueert
# de image niet meer (401 op docker.io/minio en quay.io/minio, 410 op dl.min.io), de
# GitHub-release van dezelfde tag wel, en de sha256sum ernaast vangt een foutpagina die
# anders als binary wordt opgeslagen. Deze image bouwt alleen voor amd64, net als de
# kopia-regel hierboven.
ARG MC_VERSION=RELEASE.2025-08-13T08-35-41Z
RUN MC_ASSET="mc.linux-amd64.${MC_VERSION}" \
    && MC_URL="https://github.com/minio/mc/releases/download/${MC_VERSION}/${MC_ASSET}" \
    && curl -fsSL -o /usr/local/bin/mc "${MC_URL}" \
    && curl -fsSL -o /tmp/mc.sha256sum "${MC_URL}.sha256sum" \
    && echo "$(cut -d' ' -f1 /tmp/mc.sha256sum)  /usr/local/bin/mc" | sha256sum -c - \
    && chmod 755 /usr/local/bin/mc \
    && rm /tmp/mc.sha256sum

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
