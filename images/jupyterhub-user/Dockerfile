# De pod per gebruiker van JupyterHub: JupyterLab plus VS Code.
FROM quay.io/jupyter/minimal-notebook:2026-10-12

ARG CODE_SERVER_VERSION=4.141.0
ARG VSCODE_PROXY_VERSION=0.7
ARG SERVER_PROXY_VERSION=4.6.0
ARG COLLABORATION_VERSION=5.0.4
ARG PYTHON_EXTENSION_VERSION=2026.4.0
ARG BASEDPYRIGHT_VERSION=1.40.2

# Het .deb van de release in plaats van install.sh van internet: het script verandert per bouw.
# code-server publiceert geen checksumbestand, dus deze sommen zijn van het artefact zelf berekend.
ARG TARGETARCH
ARG CODE_SERVER_SHA256_AMD64=f384beb4f3ba207e0bca27d0dbd36dd825d5aa909d78c6d4baa8703eaf6b77ba
ARG CODE_SERVER_SHA256_ARM64=0bca33a9b01180ad88f0b8969a8e34ae1c76417df471697e7cd65c20ef5dd60d

USER root
RUN case "${TARGETARCH}" in \
      amd64) SHA256="${CODE_SERVER_SHA256_AMD64}" ;; \
      arm64) SHA256="${CODE_SERVER_SHA256_ARM64}" ;; \
      *) echo "geen code-server-checksum voor TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
    esac \
 && PAKKET="/tmp/code-server_${CODE_SERVER_VERSION}_${TARGETARCH}.deb" \
 && curl -fsSL -o "${PAKKET}" "https://github.com/coder/code-server/releases/download/v${CODE_SERVER_VERSION}/code-server_${CODE_SERVER_VERSION}_${TARGETARCH}.deb" \
 && echo "${SHA256}  ${PAKKET}" | sha256sum -c - \
 && dpkg -i "${PAKKET}" \
 && rm "${PAKKET}"
USER ${NB_UID}

# jupyter-vscode-proxy 0.7 declareert geen dependencies, dus jupyter-server-proxy expliciet erbij.
RUN pip install --no-cache-dir \
      "jupyter-server-proxy==${SERVER_PROXY_VERSION}" \
      "jupyter-vscode-proxy==${VSCODE_PROXY_VERSION}" \
      "jupyter-collaboration==${COLLABORATION_VERSION}"

# Extensies van Open VSX (niet van Microsoft); basedpyright in plaats van Pylance. Geen AI-extensies.
RUN code-server --install-extension "ms-python.python@${PYTHON_EXTENSION_VERSION}" \
 && code-server --install-extension "detachhead.basedpyright@${BASEDPYRIGHT_VERSION}"

# Zet het meegebakken Copilot-chatpaneel van code-server uit.
COPY --chown=${NB_UID}:${NB_GID} code-server-settings.json /home/${NB_USER}/.local/share/code-server/User/settings.json
