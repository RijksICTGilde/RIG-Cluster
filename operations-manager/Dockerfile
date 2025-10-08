# Multi-stage Docker build for operations-manager with optimized caching
# Stage 1: Base system with dependencies (rarely changes)
FROM python:3.13-slim AS base-system

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    POETRY_VERSION=2.1.3 \
    POETRY_HOME="/opt/poetry" \
    POETRY_VIRTUALENVS_CREATE=false \
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
    postgresql-client

# Install kubectl - cached unless kubectl version changes
RUN curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.28/deb/Release.key | gpg --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v1.28/deb/ /" | tee /etc/apt/sources.list.d/kubernetes.list \
    && apt-get update && apt-get install -y kubectl

# Install SOPS - cached unless SOPS version changes
RUN SOPS_VERSION=$(curl -s https://api.github.com/repos/getsops/sops/releases/latest | grep -Po '"tag_name": "\K.*?(?=")') \
    && curl -LO "https://github.com/getsops/sops/releases/download/${SOPS_VERSION}/sops-${SOPS_VERSION}.linux.amd64" \
    && chmod +x "sops-${SOPS_VERSION}.linux.amd64" \
    && mv "sops-${SOPS_VERSION}.linux.amd64" /usr/local/bin/sops

# Install MinIO client - cached unless mc version changes
RUN curl -LO "https://dl.min.io/client/mc/release/linux-amd64/mc" \
    && chmod +x mc \
    && mv mc /usr/local/bin/mc

# Install poetry - cached unless poetry version changes
RUN curl -sSL https://install.python-poetry.org | POETRY_HOME=$POETRY_HOME POETRY_VERSION=$POETRY_VERSION python3 -
ENV PATH="$POETRY_HOME/bin:$PATH"

# Stage 2: Dependencies (changes when poetry files change)
FROM base-system AS dependencies

WORKDIR /app

# Copy the jinja-roos-components dependency first
COPY jinja-roos-components/ ./jinja-roos-components/

# Copy poetry configuration files - this triggers dependency installation only when these change
COPY operations-manager/python/pyproject.toml ./

# Generate lock file and install dependencies with cache mounts
# This is cached unless pyproject.toml or jinja-roos-components change
RUN --mount=type=cache,target=/root/.cache/pypoetry,sharing=locked \
    --mount=type=cache,target=/root/.cache/pip,sharing=locked \
    poetry lock --no-interaction --no-ansi && \
    poetry install --no-interaction --no-ansi --no-root

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
COPY operations-manager/python/.env.local /app/
COPY operations-manager/python/.env.production /app/

# Copy application code and resources (change frequently - put last for optimal caching)
COPY operations-manager/python/opi ./opi
COPY operations-manager/python/manifests ./manifests
COPY operations-manager/python/sources ./sources
COPY operations-manager/python/static ./static

# Set proper permissions for SSH keys and application files
RUN chown -R appuser:appuser /app && \
    chmod -R 755 /app && \
    chmod 644 /app/keys/git-server-key.pub && \
    chmod 600 /app/keys/git-server-key

# Switch to non-root user
USER appuser

# Expose port
EXPOSE 8000

# Set the command to run the application
CMD ["python", "-m", "uvicorn", "opi.server:app", "--host", "0.0.0.0", "--port", "8000"]