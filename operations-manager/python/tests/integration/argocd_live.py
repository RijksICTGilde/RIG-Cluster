"""Gedeelde hulp voor de groep ``argocd_live``: OPI tegen een echte ArgoCD.

``task test-cluster-up`` zet het wegwerpcluster op met de ArgoCD die het platform uitrolt,
en ``task test-argocd`` port-forwardt argocd-server, geeft het account ``opi`` een API-token
en zet de adressen in de omgeving.

Twee terugleeskanalen, beide los van de connector onder toets: de Kubernetes-API via
``kubernetes_live.Kubectl``, want die is de waarheid over wat er staat, en de API van Forgejo
via ``forgejo_live.ForgejoApi``, want daar staan de manifesten. Die laatste is met opzet
dezelfde klasse als de groep forgejo_live gebruikt; daarom leest deze groep ook
``FORGEJO_LIVE_URL`` en zijn broers.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from opi.connectors.kubectl import create_kubectl_connector
from opi.core.cluster_config import namespace_labels
from opi.core.config import settings
from opi.handlers.sops import SopsHandler
from tests.integration.live import ARGOCD

if TYPE_CHECKING:
    from tests.integration.forgejo_live import ForgejoApi
    from tests.integration.kubernetes_live import Kubectl

#: De namespace waar ArgoCD en zijn Application-CR's staan, zoals op de sandbox.
ARGO_NAMESPACE = "rig-system"

#: Alles wat deze groep maakt begint hiermee, en de opruiming gaat op dit voorvoegsel.
PREFIX = "zadlive"

#: De naam waaronder Forgejo in het cluster bereikbaar is. De toetsen pushen naar de
#: NodePort op de host; ArgoCD haalt op via deze Service, en dat zijn twee adressen voor
#: dezelfde repository.
FORGEJO_SERVICE = f"http://forgejo.{ARGO_NAMESPACE}.svc.cluster.local:3000"

#: De AGE-sleutel van de wegwerp-namespaces. De CMP-sidecar weigert te renderen zonder een
#: sleutel-Secret; de manifesten van deze groep zijn niet versleuteld, dus hij hoeft niets
#: te openen. Plaatshoudervorm, en hij bereikt alleen dit wegwerpcluster.
THROWAWAY_AGE_KEY = "AGE-SECRET-KEY-1EXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAMPLE"
THROWAWAY_AGE_PUBLIC_KEY = "age1example0example0example0example0example0example0example0exampl"


def host() -> str:
    return ARGOCD.omgeving("ARGOCD_LIVE_HOST")


def port() -> int:
    return int(ARGOCD.omgeving("ARGOCD_LIVE_PORT"))


def token() -> str:
    """Het API-token van het account ``opi``: de autorisatie waarmee OPI in productie werkt."""
    return ARGOCD.omgeving("ARGOCD_LIVE_TOKEN")


def admin_password() -> str:
    """Het wachtwoord van het lokale admin-account, voor de inlogroute van de connector."""
    return ARGOCD.omgeving("ARGOCD_LIVE_ADMIN_PASSWORD")


def use_tls() -> bool:
    """Of argocd-server TLS doet. Het wegwerpcluster zet ``insecure``, ODCN en de sandbox niet.

    Een omgevingsvariabele en geen vaste keuze, zodat dezelfde groep ook tegen een
    TLS-ArgoCD te draaien is; dat is wat op ODCN staat.
    """
    return os.environ.get("ARGOCD_LIVE_USE_TLS", "").lower() in ("1", "true", "yes")


def assert_local() -> None:
    ARGOCD.assert_local(f"{host()}:{port()}")


def repo_url_in_cluster(owner: str, name: str) -> str:
    """Het adres waarop ArgoCD deze repository bereikt."""
    return f"{FORGEJO_SERVICE}/{owner}/{name}.git"


def repository_secret_manifest(name: str, url: str, username: str, password: str) -> str:
    """Het Secret waarmee ArgoCD bij een privé-repository komt.

    Een Secret per repository matcht alleen op de EXACTE URL, dus hij noemt de URL waarmee
    de Application de bron aanwijst. In productie schrijft ``ArgoManager`` dit Secret via
    git; hier staat het in de opstelling, want wat deze groep meet is wat ArgoCD ermee doet.
    """
    return f"""apiVersion: v1
kind: Secret
metadata:
  name: {name}
  namespace: {ARGO_NAMESPACE}
  labels:
    argocd.argoproj.io/secret-type: repository
stringData:
  type: git
  url: {url}
  username: {username}
  password: {password}
"""


def workload_manifest(name: str, namespace: str, image: str, replicas: int = 1) -> str:
    """De werklast die ArgoCD uit de repository uitrolt."""
    return f"""apiVersion: apps/v1
kind: Deployment
metadata:
  name: {name}
  namespace: {namespace}
spec:
  replicas: {replicas}
  selector:
    matchLabels:
      app: {name}
  template:
    metadata:
      labels:
        app: {name}
    spec:
      containers:
        - name: app
          image: {image}
          command: ["/bin/sh", "-c"]
          args: ['while true; do sleep 5; done']
          resources:
            requests:
              memory: 16Mi
              cpu: 10m
            limits:
              memory: 32Mi
"""


@dataclass(frozen=True)
class DeployedApp:
    """De opstelling van een uitgerolde Application, zoals de toetsen hem aanspreken."""

    app: str
    app_project: str
    namespace: str
    repo: str
    repo_url: str
    workload: str


async def prepare_destination_namespace(namespace: str, kubectl: Kubectl) -> None:
    """De namespace waar ArgoCD in uitrolt, met wat de CMP-sidecar nodig heeft.

    Langs de productieweg: de labels komen uit ``namespace_labels``, ``SopsHandler`` zet het
    sleutel-Secret en de Role waarmee de sidecar precies dat Secret mag lezen. Zonder die
    drie faalt de sync op iets anders dan de manifesten: zonder het sleutel-Secret weigert
    ``render.sh`` te renderen, en zonder ``argocd.argoproj.io/managed-by`` weigert ArgoCD de
    bestemming met "namespace ... is not managed" (gemeten op de sandbox, 7 november 2026).
    """
    labels = namespace_labels(settings.CLUSTER_MANAGER)
    regels = "\n".join(f"    {sleutel}: {waarde}" for sleutel, waarde in labels.items())
    kubectl.apply(f"apiVersion: v1\nkind: Namespace\nmetadata:\n  name: {namespace}\n  labels:\n{regels}\n")
    handler = SopsHandler(create_kubectl_connector())
    stored = await handler.store_project_sops_key_in_namespace(namespace, THROWAWAY_AGE_KEY, THROWAWAY_AGE_PUBLIC_KEY)
    assert stored is True, f"sops-age-key kwam niet in namespace {namespace}"
    await handler.ensure_cmp_key_reader(namespace)


def drop_everything(app: DeployedApp, kubectl: Kubectl, forgejo: ForgejoApi) -> None:
    """Ruim de opstelling op, ook die van een afgebroken run.

    De finalizers gaan er eerst af: een Application en een AppProject van ArgoCD houden
    zichzelf vast tot de controller de cascade heeft gedaan, en een opruiming die daarop
    wacht hangt net zo lang als de valkuil die deze groep meet.
    """
    assert_local()
    for soort, naam in (("application", app.app), ("appproject", app.app_project)):
        kubectl.run(
            "patch", soort, naam, "-n", ARGO_NAMESPACE, "--type", "merge", "-p", '{"metadata":{"finalizers":[]}}'
        )
        kubectl.run("delete", soort, naam, "-n", ARGO_NAMESPACE, "--ignore-not-found=true")
    kubectl.run("delete", "secret", f"{PREFIX}-repo", "-n", ARGO_NAMESPACE, "--ignore-not-found=true")
    kubectl.run("delete", "namespace", app.namespace, "--ignore-not-found=true", "--wait=false")
    kubectl.run("wait", "--for=delete", f"namespace/{app.namespace}", "--timeout=180s")
    forgejo.drop_repo(app.repo)
