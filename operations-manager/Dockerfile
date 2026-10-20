# Multi-stage Docker build for operations-manager with optimized caching

# uv krijgt een eigen stage, want buildkit expandeert geen variabele in `COPY --from=`.
ARG UV_VERSION=0.13.1
ARG MC_VERSION=RELEASE.2025-08-13T08-35-41Z
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

# mc uit onze eigen spiegel: MinIO distribueert hem niet meer, en een build die de
# GitHub-release ophaalt hangt aan het kanaal dat als vierde kan sluiten. Waarom die spiegel
# er is staat in images/mc/Dockerfile, hoe je hem vult in images/mc/README.md.
FROM ghcr.io/minbzk/base-images/mc:${MC_VERSION} AS mc

# Stage 1: Base system with dependencies (rarely changes)
FROM python:3.14-slim AS base-system

# Pin tool versions for deterministic builds and optimal layer caching.
# Bump these ARGs when you need a newer version.
ARG SOPS_VERSION=v3.13.3
ARG KOPIA_VERSION=0.23.1
ARG CHISEL_VERSION=1.12.0

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/usr/local \
    TZ=Europe/Amsterdam \
    DEBIAN_FRONTEND=noninteractive

# Install system dependencies - this layer is cached unless system deps change.
# upgrade pulls Debian security updates to keep the container-scan gate green.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
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
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.32/deb/Release.key | gpg --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v1.32/deb/ /" | tee /etc/apt/sources.list.d/kubernetes.list \
    && apt-get update && apt-get install -y kubectl

# TARGETARCH is set automatically by Docker buildx (amd64 or arm64)
ARG TARGETARCH

# Install SOPS - layer cached unless SOPS_VERSION ARG changes
# Checksums staan literaal; zie images/README.md.
ARG SOPS_SHA256_AMD64=e5bec3346a873ae91d871550f3e698c1aad962aff462a080e40f25fde17fef6b
ARG SOPS_SHA256_ARM64=53b0abacd38ef1b12a66d6c100956691b9cefce018d91f81e73ddf7438b94d77
RUN case "${TARGETARCH}" in \
      amd64) SOPS_SHA256="${SOPS_SHA256_AMD64}" ;; \
      arm64) SOPS_SHA256="${SOPS_SHA256_ARM64}" ;; \
      *) echo "geen sops-checksum voor TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl -fsSLO "https://github.com/getsops/sops/releases/download/${SOPS_VERSION}/sops-${SOPS_VERSION}.linux.${TARGETARCH}" \
    && echo "${SOPS_SHA256}  sops-${SOPS_VERSION}.linux.${TARGETARCH}" | sha256sum -c - \
    && chmod +x "sops-${SOPS_VERSION}.linux.${TARGETARCH}" \
    && mv "sops-${SOPS_VERSION}.linux.${TARGETARCH}" /usr/local/bin/sops

# Install MinIO client
COPY --from=mc --chmod=755 /usr/bin/mc /usr/local/bin/mc

# Install Kopia - layer cached unless KOPIA_VERSION ARG changes
# Kopia uses "x64" for amd64 and "arm64" for arm64
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

# Install Skopeo - for pushing container images to remote registries
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends skopeo

# Install Chisel - layer cached unless CHISEL_VERSION ARG changes
ARG CHISEL_SHA256_AMD64=f3f180f1d93aa72cce4e6386f98cc06569a0146fbd65eb4423cf83e6434bcfe6
ARG CHISEL_SHA256_ARM64=2ec6152cd2c74fe0146d4d79e4e7aa174521368c56e433d55e023a92ea404ec3
RUN case "${TARGETARCH}" in \
      amd64) CHISEL_SHA256="${CHISEL_SHA256_AMD64}" ;; \
      arm64) CHISEL_SHA256="${CHISEL_SHA256_ARM64}" ;; \
      *) echo "geen chisel-checksum voor TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl -fsSLO "https://github.com/jpillora/chisel/releases/download/v${CHISEL_VERSION}/chisel_${CHISEL_VERSION}_linux_${TARGETARCH}.gz" \
    && echo "${CHISEL_SHA256}  chisel_${CHISEL_VERSION}_linux_${TARGETARCH}.gz" | sha256sum -c - \
    && gunzip "chisel_${CHISEL_VERSION}_linux_${TARGETARCH}.gz" \
    && chmod +x "chisel_${CHISEL_VERSION}_linux_${TARGETARCH}" \
    && mv "chisel_${CHISEL_VERSION}_linux_${TARGETARCH}" /usr/local/bin/chisel

# Install uv - cached unless uv version changes
COPY --from=uv /uv /usr/local/bin/uv

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
    (find /usr/local/lib/python3.14/site-packages -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true) && \
    (find /usr/local/lib/python3.14/site-packages -type d -name "tests" -exec rm -rf {} + 2>/dev/null || true) && \
    (find /usr/local/lib/python3.14/site-packages -type d -name "test" -exec rm -rf {} + 2>/dev/null || true)

# Stage 3: Application (changes frequently)
FROM dependencies AS application

# Bake version metadata for the /version endpoint and footer. CI passes these as
# build-args (immutable image tag / git SHA); plain builds get harmless fallbacks.
# In skaffold dev these are superseded by a hot-synced opi/version.json.
ARG ZAD_VERSION=0.1.0
ARG ZAD_GIT_COMMIT=""
ARG ZAD_GIT_BRANCH=""
ARG ZAD_BUILD_DATE=""
ENV ZAD_VERSION=${ZAD_VERSION} \
    ZAD_GIT_COMMIT=${ZAD_GIT_COMMIT} \
    ZAD_GIT_BRANCH=${ZAD_GIT_BRANCH} \
    ZAD_BUILD_DATE=${ZAD_BUILD_DATE}

# Create non-root user for OpenShift compatibility. /app zelf hoort erbij: COPY --chown
# zet alleen de eigenaar van wat hij schrijft, en WORKDIR maakte /app als root aan.
RUN groupadd -g 1001 appuser && \
    useradd -r -u 1001 -g appuser appuser && \
    chown appuser:appuser /app

# Copy Alembic configuration (for database migrations)
COPY --chown=appuser:appuser operations-manager/python/alembic.ini /app/

# Copy entrypoint script
COPY --chown=appuser:appuser operations-manager/docker-entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Copy application code and resources, van zelden naar vaak wijzigend: een laag die
# opnieuw gebouwd wordt sleept elke laag eronder mee. Zet je iets nieuws in static/, dan
# hoort het in een van deze regels; tests/test_image_layer_guard.py meet dat.
# Achtergrond en metingen: features/image-lagen.md.
COPY --chown=appuser:appuser operations-manager/python/static/promo.mp4 operations-manager/python/static/*.jpg operations-manager/python/static/favicon.ico ./static/
COPY --chown=appuser:appuser operations-manager/python/manifests ./manifests
COPY --chown=appuser:appuser operations-manager/python/static/css ./static/css
COPY --chown=appuser:appuser operations-manager/python/static/js ./static/js
COPY --chown=appuser:appuser operations-manager/python/opi ./opi

# Switch to non-root user
USER appuser

# Expose ports
EXPOSE 8000
EXPOSE 5678

# Use tini as PID 1 to properly reap zombie child processes
ENTRYPOINT ["tini", "--"]

# Set the command to run the entrypoint script (which handles migrations then starts the app)
CMD ["/app/entrypoint.sh"]
