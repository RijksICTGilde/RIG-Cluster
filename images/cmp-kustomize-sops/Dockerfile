# Multi-stage build to download tools
FROM alpine:3.19 AS tools-downloader

# TARGETARCH is set automatically by Docker buildx (amd64 or arm64)
ARG TARGETARCH

# Install curl, bash, git and other tools for downloading
RUN apk add --no-cache curl bash tar file git

# Download all tools
WORKDIR /tools

# Versies en checksums van het gereedschap. De checksums staan hier literaal: dat laat een
# gewijzigd artefact omvallen, waar een .sha256sum van dezelfde bron alleen de overdracht
# toetst. Bij een nieuwe versie horen nieuwe sommen; de hele weg staat in images/README.md.
#
# kubectl volgde eerder stable.txt, dus elke bouw een andere versie. Nu dezelfde minor als
# de OPI-image (operations-manager/Dockerfile) en als het sandbox-cluster: v1.32.
ARG KUBECTL_VERSION=v1.32.13
ARG KUBECTL_SHA256_AMD64=db2ae479a63f3665d7f704ab18c0d4d4050144237980763221835b7305703c4c
ARG KUBECTL_SHA256_ARM64=b1f87f196633a89208546d79bfa4e2470bda70e7bf42c4d3adb008ec208da9d1
ARG KUSTOMIZE_VERSION=5.8.1
ARG KUSTOMIZE_SHA256_AMD64=029a7f0f4e1932c52a0476cf02a0fd855c0bb85694b82c338fc648dcb53a819d
ARG KUSTOMIZE_SHA256_ARM64=0953ea3e476f66d6ddfcd911d750f5167b9365aa9491b2326398e289fef2c142
ARG SOPS_VERSION=v3.13.2
ARG SOPS_SHA256_AMD64=154dfe4cd70554bdd82b98e4cd4acf191d43d01ead6f00a73477aa44c4ac42ef
ARG SOPS_SHA256_ARM64=78abf2e15c86250a1553ae6f53aba96be6b2a8126f160b1534959add3467ad76
ARG YQ_VERSION=v4.54.1
ARG YQ_SHA256_AMD64=8e34fc298390875de416e6a4afcb8cabeceb25d9aa8506c1a2f9353cf702ea5f
ARG YQ_SHA256_ARM64=189088da0c6429ec5178dfaab1a114805f6cab0b61b165ab236efedf1d57a71b
ARG HELM_VERSION=v3.21.3
ARG HELM_SHA256_AMD64=15e041a93a590dce8100f39385cd98c84a765c9e36aeeb9e2dc6ff9e4769e2e0
ARG HELM_SHA256_ARM64=67f58155079ff9ffab98ba5c88daff0ed9b542f3a4732f5dd426dde7dd0f5244
ARG HELMFILE_VERSION=1.7.1
ARG HELMFILE_SHA256_AMD64=d84a8e5ffdbfd66f1bfb4b11967b9d2981d8515c7d6e312ee0cd89b613c8b352
ARG HELMFILE_SHA256_ARM64=c957e2a07469b535b427e9838e6edd30202366bd5a06e0aa3aaa6f562deeb29e
ARG HELM_DIFF_VERSION=3.15.10
ARG HELM_DIFF_SHA256_AMD64=ffeff863e4a3cbe83282a13a55ee972f7497966dfb66f326a117f9b094fff161
ARG HELM_DIFF_SHA256_ARM64=6832085986feef54b7be82906f2516f3565fdd1be11c109737e8833f1e1c0a5c
ARG KSOPS_VERSION=4.5.1
ARG KSOPS_SHA256_AMD64=9641e03a301bf2fc4b98d0e837a0351e1ca443ecd7a68b25e6af2149fbb2ef30
ARG KSOPS_SHA256_ARM64=c7bb562cc2d580a1decc6c77a35e4d35575fb01ca003425726004ecd13dc1cb5

# Download kubectl
RUN case "${TARGETARCH}" in \
      amd64) SHA256="${KUBECTL_SHA256_AMD64}" ;; \
      arm64) SHA256="${KUBECTL_SHA256_ARM64}" ;; \
      *) echo "geen kubectl-checksum voor TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl -fsSLo kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl" \
    && echo "${SHA256}  kubectl" | sha256sum -c - \
    && chmod +x kubectl

# Download kustomize
RUN case "${TARGETARCH}" in \
      amd64) SHA256="${KUSTOMIZE_SHA256_AMD64}" ;; \
      arm64) SHA256="${KUSTOMIZE_SHA256_ARM64}" ;; \
      *) echo "geen kustomize-checksum voor TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && ARCHIEF="kustomize_v${KUSTOMIZE_VERSION}_linux_${TARGETARCH}.tar.gz" \
    && curl -fsSLO "https://github.com/kubernetes-sigs/kustomize/releases/download/kustomize/v${KUSTOMIZE_VERSION}/${ARCHIEF}" \
    && echo "${SHA256}  ${ARCHIEF}" | sha256sum -c - \
    && tar xz -f "${ARCHIEF}" \
    && rm "${ARCHIEF}" \
    && chmod +x kustomize

# Download sops
RUN case "${TARGETARCH}" in \
      amd64) SHA256="${SOPS_SHA256_AMD64}" ;; \
      arm64) SHA256="${SOPS_SHA256_ARM64}" ;; \
      *) echo "geen sops-checksum voor TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl -fsSLo sops "https://github.com/getsops/sops/releases/download/${SOPS_VERSION}/sops-${SOPS_VERSION}.linux.${TARGETARCH}" \
    && echo "${SHA256}  sops" | sha256sum -c - \
    && chmod +x sops

# Download yq
RUN case "${TARGETARCH}" in \
      amd64) SHA256="${YQ_SHA256_AMD64}" ;; \
      arm64) SHA256="${YQ_SHA256_ARM64}" ;; \
      *) echo "geen yq-checksum voor TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl -fsSLo yq "https://github.com/mikefarah/yq/releases/download/${YQ_VERSION}/yq_linux_${TARGETARCH}" \
    && echo "${SHA256}  yq" | sha256sum -c - \
    && chmod +x yq

# Download helm
RUN case "${TARGETARCH}" in \
      amd64) SHA256="${HELM_SHA256_AMD64}" ;; \
      arm64) SHA256="${HELM_SHA256_ARM64}" ;; \
      *) echo "geen helm-checksum voor TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && ARCHIEF="helm-${HELM_VERSION}-linux-${TARGETARCH}.tar.gz" \
    && curl -fsSLO "https://get.helm.sh/${ARCHIEF}" \
    && echo "${SHA256}  ${ARCHIEF}" | sha256sum -c - \
    && tar xz -f "${ARCHIEF}" \
    && mv linux-${TARGETARCH}/helm . \
    && rm -rf "${ARCHIEF}" linux-${TARGETARCH} \
    && chmod +x helm

# Download helmfile
RUN case "${TARGETARCH}" in \
      amd64) SHA256="${HELMFILE_SHA256_AMD64}" ;; \
      arm64) SHA256="${HELMFILE_SHA256_ARM64}" ;; \
      *) echo "geen helmfile-checksum voor TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && ARCHIEF="helmfile_${HELMFILE_VERSION}_linux_${TARGETARCH}.tar.gz" \
    && curl -fsSLO "https://github.com/helmfile/helmfile/releases/download/v${HELMFILE_VERSION}/${ARCHIEF}" \
    && echo "${SHA256}  ${ARCHIEF}" | sha256sum -c - \
    && tar xz -f "${ARCHIEF}" \
    && rm "${ARCHIEF}" \
    && chmod +x helmfile

# Install helm-diff plugin (required by helmfile)
# Download pre-built release directly instead of using helm plugin install (faster)
# The tarball extracts to diff/, so we extract to plugins/ and rename
ENV HELM_DATA_HOME=/tools/helm-data
RUN case "${TARGETARCH}" in \
      amd64) SHA256="${HELM_DIFF_SHA256_AMD64}" ;; \
      arm64) SHA256="${HELM_DIFF_SHA256_ARM64}" ;; \
      *) echo "geen helm-diff-checksum voor TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && ARCHIEF="helm-diff-linux-${TARGETARCH}.tgz" \
    && mkdir -p $HELM_DATA_HOME/plugins \
    && curl -fsSLO "https://github.com/databus23/helm-diff/releases/download/v${HELM_DIFF_VERSION}/${ARCHIEF}" \
    && echo "${SHA256}  ${ARCHIEF}" | sha256sum -c - \
    && tar xz -f "${ARCHIEF}" -C $HELM_DATA_HOME/plugins \
    && rm "${ARCHIEF}" \
    && mv $HELM_DATA_HOME/plugins/diff $HELM_DATA_HOME/plugins/helm-diff

# Download KSOPS plugin
# KSOPS uses x86_64 instead of amd64 in release filenames
RUN case "${TARGETARCH}" in \
      amd64) KSOPS_ARCH=x86_64; SHA256="${KSOPS_SHA256_AMD64}" ;; \
      arm64) KSOPS_ARCH=arm64; SHA256="${KSOPS_SHA256_ARM64}" ;; \
      *) echo "geen ksops-checksum voor TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && ARCHIEF="ksops_${KSOPS_VERSION}_Linux_${KSOPS_ARCH}.tar.gz" \
    && curl -fsSLO "https://github.com/viaduct-ai/kustomize-sops/releases/download/v${KSOPS_VERSION}/${ARCHIEF}" \
    && echo "${SHA256}  ${ARCHIEF}" | sha256sum -c - \
    && tar xz -f "${ARCHIEF}" -C /tools \
    && rm "${ARCHIEF}" \
    && chmod +x /tools/ksops

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
FROM quay.io/argoproj/argocd:v2.14.21

# Switch to root to install packages and setup tools
USER root

# Install additional runtime dependencies and pick up Ubuntu security patches
# newer than the ArgoCD base image
RUN apt-get update && apt-get upgrade -y && apt-get install -y \
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
