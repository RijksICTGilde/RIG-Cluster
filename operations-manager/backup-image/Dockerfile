# Unified backup image for RIG Cluster
# Supports: PVC backups (kopia), PostgreSQL backups (pg_dump/pg_restore), MinIO backups (mc)
# Target image: ghcr.io/minbzk/base-images/rig-backup:latest

FROM python:3.13-slim

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
RUN KOPIA_VERSION=$(curl -s https://api.github.com/repos/kopia/kopia/releases/latest | grep -Po '"tag_name": "v\K[^"]*') \
    && curl -LO "https://github.com/kopia/kopia/releases/download/v${KOPIA_VERSION}/kopia-${KOPIA_VERSION}-linux-x64.tar.gz" \
    && tar -xzf "kopia-${KOPIA_VERSION}-linux-x64.tar.gz" \
    && mv "kopia-${KOPIA_VERSION}-linux-x64/kopia" /usr/local/bin/kopia \
    && chmod +x /usr/local/bin/kopia \
    && rm -rf "kopia-${KOPIA_VERSION}-linux-x64.tar.gz" "kopia-${KOPIA_VERSION}-linux-x64"

# Install MinIO client - for bucket operations
RUN curl -LO "https://dl.min.io/client/mc/release/linux-amd64/mc" \
    && chmod +x mc \
    && mv mc /usr/local/bin/mc

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
