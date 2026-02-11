# Multi-stage build to download tools
FROM alpine:3.19 AS tools-downloader

# TARGETARCH is set automatically by Docker buildx (amd64 or arm64)
ARG TARGETARCH

# Install curl, bash, git and other tools for downloading
RUN apk add --no-cache curl bash tar file git

# Download all tools
WORKDIR /tools

# Download kubectl
RUN curl -Lo kubectl "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/${TARGETARCH}/kubectl" && \
    chmod +x kubectl

# Download kustomize
RUN KUSTOMIZE_VERSION="5.8.1" && \
    curl -fsSL "https://github.com/kubernetes-sigs/kustomize/releases/download/kustomize/v${KUSTOMIZE_VERSION}/kustomize_v${KUSTOMIZE_VERSION}_linux_${TARGETARCH}.tar.gz" | tar xz && \
    chmod +x kustomize

# Download sops
RUN curl -Lo sops "https://github.com/getsops/sops/releases/download/v3.9.2/sops-v3.9.2.linux.${TARGETARCH}" && \
    chmod +x sops

# Download yq
RUN curl -Lo yq "https://github.com/mikefarah/yq/releases/latest/download/yq_linux_${TARGETARCH}" && \
    chmod +x yq

# Download helm
RUN curl -fsSL "https://get.helm.sh/helm-v3.19.4-linux-${TARGETARCH}.tar.gz" | tar xz && \
    mv linux-${TARGETARCH}/helm . && \
    rm -rf linux-${TARGETARCH} && \
    chmod +x helm

# Download helmfile
RUN curl -fsSL "https://github.com/helmfile/helmfile/releases/download/v0.169.2/helmfile_0.169.2_linux_${TARGETARCH}.tar.gz" | tar xz && \
    chmod +x helmfile

# Install helm-diff plugin (required by helmfile)
# Download pre-built release directly instead of using helm plugin install (faster)
# The tarball extracts to diff/, so we extract to plugins/ and rename
ENV HELM_DATA_HOME=/tools/helm-data
RUN mkdir -p $HELM_DATA_HOME/plugins && \
    curl -fsSL "https://github.com/databus23/helm-diff/releases/download/v3.9.12/helm-diff-linux-${TARGETARCH}.tgz" | \
    tar xz -C $HELM_DATA_HOME/plugins && \
    mv $HELM_DATA_HOME/plugins/diff $HELM_DATA_HOME/plugins/helm-diff && \
    ls -la $HELM_DATA_HOME/plugins/helm-diff/

# Download KSOPS plugin - pinned to v4.3.2 for stability
# KSOPS uses x86_64 instead of amd64 in release filenames
RUN KSOPS_VERSION="4.3.2" && \
    KSOPS_ARCH="${TARGETARCH}" && \
    if [ "$KSOPS_ARCH" = "amd64" ]; then KSOPS_ARCH="x86_64"; fi && \
    echo "Downloading KSOPS v${KSOPS_VERSION} for ${KSOPS_ARCH}..." && \
    curl -fsSL "https://github.com/viaduct-ai/kustomize-sops/releases/download/v${KSOPS_VERSION}/ksops_${KSOPS_VERSION}_Linux_${KSOPS_ARCH}.tar.gz" | tar xz -C /tools && \
    chmod +x /tools/ksops && \
    echo "KSOPS installation complete" && \
    ls -la /tools/ksops && \
    echo "KSOPS verification:" && \
    /tools/ksops --help 2>&1 | head -5

# Verify all tools downloaded
RUN echo "=== Downloaded tools verification ===" && \
    echo "Target architecture: ${TARGETARCH}" && \
    echo "Directory contents:" && ls -la /tools/ && \
    echo "" && \
    echo "kubectl test:" && ./kubectl version --client && \
    echo "kustomize test:" && ./kustomize version && \
    echo "sops test:" && ./sops --version && \
    echo "yq test:" && ./yq --version && \
    echo "helm test:" && ./helm version && \
    echo "helmfile test:" && ./helmfile --version && \
    echo "" && \
    echo "=== KSOPS detailed check ===" && \
    echo "KSOPS file info:" && ls -la ./ksops && \
    echo "KSOPS file type:" && file ./ksops && \
    echo "KSOPS execution test:" && (./ksops --help 2>&1 | head -3 || echo "KSOPS execution failed in downloader stage") && \
    echo "=== Downloader verification complete ==="

# Final image based on ArgoCD
FROM quay.io/argoproj/argocd:v2.8.4

# Switch to root to install packages and setup tools
USER root

# Install additional runtime dependencies
RUN apt-get update && apt-get install -y \
    jq \
    file \
    && rm -rf /var/lib/apt/lists/*

# Copy all tools from downloader stage
COPY --from=tools-downloader /tools/kubectl /usr/local/bin/kubectl
COPY --from=tools-downloader /tools/kustomize /usr/local/bin/kustomize
COPY --from=tools-downloader /tools/sops /usr/local/bin/sops
COPY --from=tools-downloader /tools/yq /usr/local/bin/yq
COPY --from=tools-downloader /tools/helm /usr/local/bin/helm
COPY --from=tools-downloader /tools/helmfile /usr/local/bin/helmfile
COPY --from=tools-downloader /tools/ksops /usr/local/bin/ksops

# Copy helm plugins (including helm-diff required by helmfile)
COPY --from=tools-downloader /tools/helm-data /home/argocd/.local/share/helm

# Create kustomize plugin directory structure for KSOPS
RUN mkdir -p /home/argocd/.config/kustomize/plugin/viaduct.ai/v1/ksops

# Install KSOPS as kustomize plugin
RUN cp /usr/local/bin/ksops /home/argocd/.config/kustomize/plugin/viaduct.ai/v1/ksops/ksops

# Set proper ownership for argocd user
RUN chown -R 999:999 /home/argocd/.config /home/argocd/.local

# Create build info file
RUN echo "Image built on: $(date)" > /info.txt && \
    echo "KSOPS version: $(ksops --version 2>/dev/null || echo 'unknown')" >> /info.txt

# Set environment variables for KSOPS, kustomize plugins, and helm
ENV XDG_CONFIG_HOME=/home/argocd/.config
ENV KUSTOMIZE_PLUGIN_HOME=/home/argocd/.config/kustomize/plugin
ENV HELM_DATA_HOME=/home/argocd/.local/share/helm
ENV PATH=/usr/local/bin:$PATH

# Switch back to argocd user
USER 999

# Final verification of all tools and plugin setup
RUN echo "=== Final tool verification ===" && \
    echo "Current user: $(whoami)" && \
    echo "Current directory: $(pwd)" && \
    echo "PATH: $PATH" && \
    echo "" && \
    echo "=== Tool binaries ===" && \
    echo "kubectl:" && (kubectl version --client || echo "kubectl FAILED") && \
    echo "kustomize:" && (kustomize version || echo "kustomize FAILED") && \
    echo "sops:" && (sops --version || echo "sops FAILED") && \
    echo "yq:" && (yq --version || echo "yq FAILED") && \
    echo "helm:" && (helm version || echo "helm FAILED") && \
    echo "helmfile:" && (helmfile --version || echo "helmfile FAILED") && \
    echo "jq:" && (jq --version || echo "jq FAILED") && \
    echo "" && \
    echo "=== Helm plugins ===" && \
    echo "helm plugin list:" && (helm plugin list || echo "helm plugin list FAILED") && \
    echo "" && \
    echo "=== KSOPS specific checks ===" && \
    echo "ksops binary location:" && ls -la /usr/local/bin/ksops && \
    echo "ksops file type:" && file /usr/local/bin/ksops && \
    echo "ksops test run:" && (/usr/local/bin/ksops --help 2>&1 | head -5 || echo "ksops execution FAILED") && \
    echo "" && \
    echo "=== Plugin directory ===" && \
    echo "ksops plugin:" && ls -la /home/argocd/.config/kustomize/plugin/viaduct.ai/v1/ksops/ && \
    echo "plugin file:" && file /home/argocd/.config/kustomize/plugin/viaduct.ai/v1/ksops/ksops && \
    echo "" && \
    echo "=== Environment variables ===" && \
    echo "  XDG_CONFIG_HOME=$XDG_CONFIG_HOME" && \
    echo "  KUSTOMIZE_PLUGIN_HOME=$KUSTOMIZE_PLUGIN_HOME" && \
    echo "  HELM_DATA_HOME=$HELM_DATA_HOME" && \
    echo "  PATH=$PATH" && \
    echo "=== Verification complete ==="

WORKDIR /home/argocd
