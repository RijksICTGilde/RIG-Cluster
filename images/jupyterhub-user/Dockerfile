# De pod per gebruiker van JupyterHub: JupyterLab plus VS Code.
FROM quay.io/jupyter/minimal-notebook:2026-10-12

ARG CODE_SERVER_VERSION=4.141.0
ARG VSCODE_PROXY_VERSION=0.7
ARG SERVER_PROXY_VERSION=4.6.0
ARG COLLABORATION_VERSION=5.0.4
ARG PYTHON_EXTENSION_VERSION=2026.4.0
ARG BASEDPYRIGHT_VERSION=1.40.2

USER root
RUN curl -fsSL https://code-server.dev/install.sh | sh -s -- --version "${CODE_SERVER_VERSION}"
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
