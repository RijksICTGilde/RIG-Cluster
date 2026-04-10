# Multi-stage Docker build for operations-manager with optimized caching
# Stage 1: Base system with dependencies (rarely changes)
FROM python:3.14-slim AS base-system

# Pin tool versions for deterministic builds and optimal layer caching.
# Bump these ARGs when you need a newer version.
ARG SOPS_VERSION=v3.12.1
ARG KOPIA_VERSION=0.22.3
ARG CHISEL_VERSION=1.11.3

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/usr/local \
    TZ=Europe/Amsterdam \
    DEBIAN_FRONTEND=noninteractive

# Install system dependencies - this layer is cached unless system deps change
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
    git \
    openssh-client \
    curl \
    apt-transport-https \
    gnupg2 \
    ca-certificates \
    age \
    tini \
    postgresql-client

# Install kubectl - cached unless kubectl version changes
RUN curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.32/deb/Release.key | gpg --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v1.32/deb/ /" | tee /etc/apt/sources.list.d/kubernetes.list \
    && apt-get update && apt-get install -y kubectl

# TARGETARCH is set automatically by Docker buildx (amd64 or arm64)
ARG TARGETARCH

# Install SOPS - layer cached unless SOPS_VERSION ARG changes
RUN curl -LO "https://github.com/getsops/sops/releases/download/${SOPS_VERSION}/sops-${SOPS_VERSION}.linux.${TARGETARCH}" \
    && chmod +x "sops-${SOPS_VERSION}.linux.${TARGETARCH}" \
    && mv "sops-${SOPS_VERSION}.linux.${TARGETARCH}" /usr/local/bin/sops

# Install MinIO client - cached unless mc version changes
RUN curl -LO "https://dl.min.io/client/mc/release/linux-${TARGETARCH}/mc" \
    && chmod +x mc \
    && mv mc /usr/local/bin/mc

# Install Kopia - layer cached unless KOPIA_VERSION ARG changes
# Kopia uses "x64" for amd64 and "arm64" for arm64
RUN KOPIA_ARCH=$(echo "${TARGETARCH}" | sed 's/amd64/x64/') \
    && curl -LO "https://github.com/kopia/kopia/releases/download/v${KOPIA_VERSION}/kopia-${KOPIA_VERSION}-linux-${KOPIA_ARCH}.tar.gz" \
    && tar -xzf "kopia-${KOPIA_VERSION}-linux-${KOPIA_ARCH}.tar.gz" \
    && mv "kopia-${KOPIA_VERSION}-linux-${KOPIA_ARCH}/kopia" /usr/local/bin/kopia \
    && chmod +x /usr/local/bin/kopia \
    && rm -rf "kopia-${KOPIA_VERSION}-linux-${KOPIA_ARCH}.tar.gz" "kopia-${KOPIA_VERSION}-linux-${KOPIA_ARCH}"

# Install Skopeo - for pushing container images to remote registries
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends skopeo

# Install Chisel - layer cached unless CHISEL_VERSION ARG changes
RUN curl -LO "https://github.com/jpillora/chisel/releases/download/v${CHISEL_VERSION}/chisel_${CHISEL_VERSION}_linux_${TARGETARCH}.gz" \
    && gunzip "chisel_${CHISEL_VERSION}_linux_${TARGETARCH}.gz" \
    && chmod +x "chisel_${CHISEL_VERSION}_linux_${TARGETARCH}" \
    && mv "chisel_${CHISEL_VERSION}_linux_${TARGETARCH}" /usr/local/bin/chisel

# Install uv - cached unless uv version changes
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Stage 2: Dependencies (changes when lock file changes)
FROM base-system AS dependencies

WORKDIR /app

# Copy uv configuration files - this triggers dependency installation only when these change
COPY operations-manager/python/pyproject.toml operations-manager/python/uv.lock ./

# Install only production dependencies (exclude dev and test groups)
# UV_PROJECT_ENVIRONMENT=/usr/local installs into system Python directly
# Then clean up cache and unnecessary files to reduce image size
RUN uv sync --frozen --no-install-project --no-dev --no-group test && \
    rm -rf /root/.cache/uv && \
    find /usr/local/lib/python3.13/site-packages -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true && \
    find /usr/local/lib/python3.13/site-packages -type d -name "tests" -exec rm -rf {} + 2>/dev/null || true && \
    find /usr/local/lib/python3.13/site-packages -type d -name "test" -exec rm -rf {} + 2>/dev/null || true

# Stage 3: Application (changes frequently)
FROM dependencies AS application

# Create non-root user for OpenShift compatibility
RUN groupadd -g 1001 appuser && \
    useradd -r -u 1001 -g appuser appuser

# Create directory for SSH keys
RUN mkdir -p /app/keys

# Copy SSH keys (rarely change)
COPY keys/git-server-key keys/git-server-key.pub /app/keys/

# Copy specific environment files (rarely change)
COPY operations-manager/python/.env /app/
COPY operations-manager/python/.env.production /app/

# Copy Alembic configuration (for database migrations)
COPY operations-manager/python/alembic.ini /app/

# Copy entrypoint script
COPY operations-manager/docker-entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Copy application code and resources (change frequently - put last for optimal caching)
COPY operations-manager/python/opi ./opi
COPY operations-manager/python/manifests ./manifests
COPY operations-manager/python/extensions ./extensions
COPY operations-manager/python/sources ./sources
COPY operations-manager/python/static ./static

# Set proper permissions for SSH keys and application files
RUN chown -R appuser:appuser /app && \
    chmod -R 755 /app && \
    chmod 644 /app/keys/git-server-key.pub && \
    chmod 600 /app/keys/git-server-key

# Switch to non-root user
USER appuser

# Expose ports
EXPOSE 8000
EXPOSE 5678

# Use tini as PID 1 to properly reap zombie child processes
ENTRYPOINT ["tini", "--"]

# Set the command to run the entrypoint script (which handles migrations then starts the app)
CMD ["/app/entrypoint.sh"]
