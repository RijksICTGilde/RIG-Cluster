# Base 5.5.2: kubespawner 7.1.0 (juli 2026) is ouder dan JupyterHub 6.0.0 (september 2026).
FROM quay.io/jupyterhub/jupyterhub:5.5.2

ARG KUBESPAWNER_VERSION=7.1.0
ARG OAUTHENTICATOR_VERSION=17.4.0
ARG IDLE_CULLER_VERSION=2.0.0

RUN python3 -m pip install --no-cache-dir \
      "jupyterhub-kubespawner==${KUBESPAWNER_VERSION}" \
      "oauthenticator==${OAUTHENTICATOR_VERSION}" \
      "jupyterhub-idle-culler==${IDLE_CULLER_VERSION}"

COPY jupyterhub_config.py /srv/jupyterhub/jupyterhub_config.py

# De pod-security-context van ZAD dwingt runAsNonRoot af (uid 1001).
RUN chown -R 1001:1001 /srv/jupyterhub
USER 1001

EXPOSE 8000 8081

CMD ["jupyterhub", "-f", "/srv/jupyterhub/jupyterhub_config.py"]
