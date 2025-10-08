# Multi-stage build to download tools
FROM alpine:3.19 AS tools-downloader

# Install curl and bash for downloading tools
RUN apk add --no-cache curl bash tar file

# Download all tools
WORKDIR /tools

# Download kubectl
RUN curl -Lo kubectl "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" && \
    chmod +x kubectl

# Download kustomize
RUN curl -s "https://raw.githubusercontent.com/kubernetes-sigs/kustomize/master/hack/install_kustomize.sh" | bash -s -- /tools

# Download sops
RUN curl -Lo sops https://github.com/mozilla/sops/releases/download/v3.8.1/sops-v3.8.1.linux.amd64 && \
    chmod +x sops

# Download yq
RUN curl -Lo yq https://github.com/mikefarah/yq/releases/latest/download/yq_linux_amd64 && \
    chmod +x yq

# Download KSOPS plugin using the official install script
RUN echo "Downloading KSOPS using official install script..." && \
    curl -s https://raw.githubusercontent.com/viaduct-ai/kustomize-sops/master/scripts/install-ksops-archive.sh | bash && \
    echo "KSOPS installation complete" && \
    echo "Copying ksops to tools directory:" && \
    cp /usr/local/bin/ksops /tools/ksops && \
    ls -la /tools/ksops && \
    echo "KSOPS verification:" && \
    /tools/ksops --help 2>&1 | head -5

# Verify all tools downloaded
RUN echo "=== Downloaded tools verification ===" && \
    echo "Directory contents:" && ls -la /tools/ && \
    echo "" && \
    echo "kubectl test:" && ./kubectl version --client && \
    echo "kustomize test:" && ./kustomize version && \
    echo "sops test:" && ./sops --version && \
    echo "yq test:" && ./yq --version && \
    echo "" && \
    echo "=== KSOPS detailed check ===" && \
    echo "KSOPS file info:" && ls -la ./ksops && \
    echo "KSOPS file type:" && file ./ksops && \
    echo "KSOPS size:" && du -h ./ksops && \
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
COPY --from=tools-downloader /tools/ksops /usr/local/bin/ksops

# Create kustomize plugin directory structure for KSOPS
RUN mkdir -p /home/argocd/.config/kustomize/plugin/viaduct.ai/v1/ksops

# Install KSOPS as kustomize plugin
RUN cp /usr/local/bin/ksops /home/argocd/.config/kustomize/plugin/viaduct.ai/v1/ksops/ksops

# Set proper ownership for argocd user
RUN chown -R 999:999 /home/argocd/.config

# Create build info file
RUN echo "Image built on: $(date)" > /info.txt && \
    echo "KSOPS version: $(ksops --version 2>/dev/null || echo 'unknown')" >> /info.txt

# Set environment variables for KSOPS and kustomize plugins
ENV XDG_CONFIG_HOME=/home/argocd/.config
ENV KUSTOMIZE_PLUGIN_HOME=/home/argocd/.config/kustomize/plugin
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
    echo "jq:" && (jq --version || echo "jq FAILED") && \
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
    echo "  PATH=$PATH" && \
    echo "=== Verification complete ==="

WORKDIR /home/argocd