"""JupyterHub-config: env vars van keycloak, publish-on-web, persistent-storage, minio en het project."""

import os
import sys

c = get_config()  # noqa: F821  (traitlets zet get_config() klaar in dit bestand)


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"env var {name} is leeg; de hub kan zonder deze waarde niet starten")
    return value


# ---------------------------------------------------------------- opslag en database

DATA_PATH = os.environ.get("DATA_PATH", "/data")

c.JupyterHub.db_url = f"sqlite:///{DATA_PATH}/jupyterhub.sqlite"
c.JupyterHub.cookie_secret_file = f"{DATA_PATH}/jupyterhub_cookie_secret"

# ---------------------------------------------------------------- netwerk

PUBLIC_HOSTNAME = _required("PUBLIC_HOSTNAME")
HUB_SERVICE_NAME = _required("JUPYTERHUB_POC_HUB_SERVICE")

c.JupyterHub.bind_url = "http://:8000"
c.JupyterHub.hub_bind_url = "http://0.0.0.0:8081"
# Via de ClusterIP-Service, want het pod-IP van de hub verandert bij elke herstart.
c.JupyterHub.hub_connect_url = f"http://{HUB_SERVICE_NAME}:8081"

# ---------------------------------------------------------------- authenticatie

OIDC_URL = _required("OIDC_URL").rstrip("/")
OIDC_REALM = _required("OIDC_REALM")
_realm_url = f"{OIDC_URL}/realms/{OIDC_REALM}/protocol/openid-connect"

# GenericOAuthenticator kent geen discovery; afgeleid, zodat de config geen HTTP-fetch doet.
c.JupyterHub.authenticator_class = "generic-oauth"
c.GenericOAuthenticator.client_id = _required("OIDC_CLIENT_ID")
c.GenericOAuthenticator.client_secret = _required("OIDC_CLIENT_SECRET")
c.GenericOAuthenticator.oauth_callback_url = f"https://{PUBLIC_HOSTNAME}/hub/oauth_callback"
c.GenericOAuthenticator.authorize_url = f"{_realm_url}/auth"
c.GenericOAuthenticator.token_url = f"{_realm_url}/token"
c.GenericOAuthenticator.userdata_url = f"{_realm_url}/userinfo"
c.GenericOAuthenticator.logout_redirect_url = f"{_realm_url}/logout"
c.GenericOAuthenticator.scope = ["openid", "profile", "email"]
c.GenericOAuthenticator.username_claim = "preferred_username"
# De poort is de realm-rol in Keycloak (restrict-access): allow_all vertrouwt de authenticator.
c.GenericOAuthenticator.allow_all = True
c.Authenticator.auto_login = True

ADMIN_USERS = {u.strip() for u in os.environ.get("JUPYTERHUB_POC_ADMINS", "").split(",") if u.strip()}
if ADMIN_USERS:
    c.Authenticator.admin_users = ADMIN_USERS

# ---------------------------------------------------------------- spawner

# De standaard (True) stopt bij het afsluiten van de hub elke draaiende server, dus elke
# rollout van dit component zou de user-pods weghalen. cleanup_proxy blijft wel aan: de
# proxy is een subproces van deze pod en hoort met de hub te verdwijnen.
c.JupyterHub.cleanup_servers = False

c.JupyterHub.spawner_class = "kubespawner.KubeSpawner"
c.KubeSpawner.image = _required("JUPYTERHUB_POC_USER_IMAGE")
c.KubeSpawner.image_pull_policy = "IfNotPresent"
c.KubeSpawner.default_url = "/lab"
c.KubeSpawner.start_timeout = 300
c.KubeSpawner.http_timeout = 120

# De tenant-netpol selecteert op het podlabel `deployment`; `app` mag hier NIET staan,
# want daarop selecteren de Service en de Deployment van de hub zelf.
c.KubeSpawner.extra_labels = {"deployment": _required("JUPYTERHUB_POC_DEPLOYMENT_LABEL")}

# Een user-pod draait code van een gebruiker en hoort niets van Kubernetes te kunnen.
c.KubeSpawner.automount_service_account_token = False
c.KubeSpawner.service_account = None
# fs_gid wordt de fsGroup in de pod-context; de container-context staat eronder.
c.KubeSpawner.fs_gid = 100
# runAsUser 1000 / runAsGroup 100 is de `jovyan` van het notebook-image, niet de 1001 van
# manifests/pod-security-context.yaml.jinja: op 1001 is /home/jovyan niet schrijfbaar.
c.KubeSpawner.container_security_context = {
    "allowPrivilegeEscalation": False,
    "capabilities": {"drop": ["ALL"]},
    "runAsNonRoot": True,
    "runAsUser": 1000,
    "runAsGroup": 100,
    "seccompProfile": {"type": "RuntimeDefault"},
}

c.KubeSpawner.cpu_limit = float(os.environ.get("JUPYTERHUB_POC_USER_CPU_LIMIT", "1"))
c.KubeSpawner.cpu_guarantee = float(os.environ.get("JUPYTERHUB_POC_USER_CPU_GUARANTEE", "0.1"))
c.KubeSpawner.mem_limit = os.environ.get("JUPYTERHUB_POC_USER_MEM_LIMIT", "2G")
c.KubeSpawner.mem_guarantee = os.environ.get("JUPYTERHUB_POC_USER_MEM_GUARANTEE", "512M")

# delete_pvc geldt bij het verwijderen van een GEBRUIKER, niet bij het stoppen van een
# server: een gecullde server laat zijn PVC dus staan.
c.KubeSpawner.storage_pvc_ensure = True
c.KubeSpawner.storage_capacity = os.environ.get("JUPYTERHUB_POC_USER_STORAGE", "1Gi")
c.KubeSpawner.storage_access_modes = ["ReadWriteOnce"]
c.KubeSpawner.delete_pvc = True

# De MinIO-bucket van het project, zodat beide gebruikers dezelfde dataset lezen. Alle
# acht namen komen uit het dienstsecret (MinIOSecret.to_k8s_secret_data), via envFrom.
_PASSTHROUGH_ENV = (
    "OBJECT_STORE_HOST",
    "OBJECT_STORE_PORT",
    "OBJECT_STORE_USER",
    "OBJECT_STORE_PASSWORD",
    "OBJECT_STORE_BUCKET_NAME",
    "OBJECT_STORE_REGION",
    "OBJECT_STORE_URL",
    "OBJECT_STORE_ENDPOINT_URL",
)
c.KubeSpawner.environment = {name: os.environ[name] for name in _PASSTHROUGH_ENV if os.environ.get(name)}

# ---------------------------------------------------------------- rollen en delen

CULL_TIMEOUT = int(os.environ.get("JUPYTERHUB_POC_CULL_TIMEOUT", "3600"))
CULL_EVERY = int(os.environ.get("JUPYTERHUB_POC_CULL_EVERY", str(max(60, CULL_TIMEOUT // 4))))

c.JupyterHub.services = [
    {
        "name": "idle-culler",
        "command": [
            sys.executable,
            "-m",
            "jupyterhub_idle_culler",
            f"--timeout={CULL_TIMEOUT}",
            f"--cull-every={CULL_EVERY}",
        ],
    }
]

c.JupyterHub.load_roles = [
    # JupyterHub 5 sharing: zonder `shares!user` weigert de hub elke share, zonder
    # `read:users:name` kan een gebruiker de ontvanger niet op naam opgeven.
    {"name": "user", "scopes": ["self", "shares!user", "read:users:name"]},
    {
        "name": "idle-culler",
        "scopes": ["list:users", "read:users:activity", "read:servers", "delete:servers"],
        "services": ["idle-culler"],
    },
]
