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

# Install Kopia - for backup repository operations
RUN KOPIA_VERSION=$(curl -s https://api.github.com/repos/kopia/kopia/releases/latest | grep -Po '"tag_name": "v\K[^"]*') \
    && curl -LO "https://github.com/kopia/kopia/releases/download/v${KOPIA_VERSION}/kopia-${KOPIA_VERSION}-linux-x64.tar.gz" \
    && tar -xzf "kopia-${KOPIA_VERSION}-linux-x64.tar.gz" \
    && mv "kopia-${KOPIA_VERSION}-linux-x64/kopia" /usr/local/bin/kopia \
    && chmod +x /usr/local/bin/kopia \
    && rm -rf "kopia-${KOPIA_VERSION}-linux-x64.tar.gz" "kopia-${KOPIA_VERSION}-linux-x64"

# Install Chisel - used for establishing HTTP tunnels to remote services
RUN CHISEL_VERSION=$(curl -s https://api.github.com/repos/jpillora/chisel/releases/latest | grep -Po '"tag_name": "v?\K[^"]*') \
    && curl -LO "https://github.com/jpillora/chisel/releases/download/v${CHISEL_VERSION}/chisel_${CHISEL_VERSION}_linux_amd64.gz" \
    && gunzip "chisel_${CHISEL_VERSION}_linux_amd64.gz" \
    && chmod +x "chisel_${CHISEL_VERSION}_linux_amd64" \
    && mv "chisel_${CHISEL_VERSION}_linux_amd64" /usr/local/bin/chisel

# Install poetry - cached unless poetry version changes
RUN curl -sSL https://install.python-poetry.org | POETRY_HOME=$POETRY_HOME POETRY_VERSION=$POETRY_VERSION python3 -
ENV PATH="$POETRY_HOME/bin:$PATH"

# Stage 2: Dependencies (changes when poetry files change)
FROM base-system AS dependencies

WORKDIR /app

# Copy poetry configuration files - this triggers dependency installation only when these change
COPY operations-manager/python/pyproject.toml operations-manager/python/poetry.lock ./

# Install only production dependencies (exclude dev and test groups)
# Then clean up pip cache and unnecessary files to reduce image size
RUN poetry install --no-interaction --no-ansi --no-root --without dev,test && \
    rm -rf /root/.cache/pip /root/.cache/pypoetry && \
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

# Expose ports
EXPOSE 8000
EXPOSE 5678

# Set the command to run the application (with conditional startup logic)
CMD ["python", "-m", "opi.server"]