# Unified backup image for RIG Cluster
# Supports: PVC backups (kopia), PostgreSQL backups (pg_dump/pg_restore), MinIO backups (mc)
# Target image: ghcr.io/minbzk/base-images/rig-backup:latest

# dl.min.io levert mc niet meer (410 Gone, het project is gearchiveerd); de image op quay wel.
ARG MC_VERSION=RELEASE.2025-08-13T08-35-41Z
FROM quay.io/minio/mc:${MC_VERSION} AS mc

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
