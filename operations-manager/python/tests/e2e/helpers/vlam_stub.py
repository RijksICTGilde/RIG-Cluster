"""De VLAM-stub die de sandbox nodig heeft om de vlam-keten echt te lopen (RC-144).

De clusterconfiguratie van `sandboxed-local` draagt een PLAATSHOUDER-endpoint: een adres
en een netwerkpeer die naar een project wijzen dat daar niet bestaat. Daarmee is de
bedrading te doorlopen (kaart, variabele, netwerkregel) maar de keten niet: een pod die
het adres gebruikt krijgt niets terug. Deze module zet er iets achter dat wel antwoordt --
een haproxy die statisch een modellenlijst teruggeeft -- op precies die coordinaten.

WAAROM DIT MET kubectl GAAT EN NIET ALS ZAD-PROJECT.
De plaatshouder noemt het project `vlam-wt8`, en die naam moet exact kloppen: hij zit in
het pod-label `project` waar de uitgaande netwerkregel van de afnemer op selecteert. Een
technische projectnaam is op dit platform NIET te kiezen -- `generate_project_name()` plakt
er een WILLEKEURIG postfix van drie tekens achter, op elke aanmaakweg (wizard en
`POST /api/v2/projects` gebruiken dezelfde functie). Een project dat via de wizard wordt
aangemaakt heet dus `vlam-a7k` en niet `vlam-wt8`, en dan selecteert de netwerkregel van de
afnemer een pod-label dat de stub niet draagt. De stub kan hier daarom alleen bestaan als
kale manifesten. Zie de PR van RC-144 voor de afweging.

Wat wel echt van OPI komt is de INKOMENDE regel: die wordt gerenderd door de
cross-domain-access-dienst zelf (dezelfde `contribute_deployment_manifests` die op productie
draait), met dezelfde wildcard-regel die in het echte `vlam-wt8` staat. Daarnaast zet deze
module een dichte deur neer die de tenant-baseline nabootst, want zonder een regel die iets
DICHT doet is een regel die iets OPEN zet niet te meten: een namespace zonder enige
NetworkPolicy laat al het inkomende verkeer door en de wildcard-regel zou dan niets bewijzen.
"""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING

import yaml
from opi.core.cluster_config import get_vlam_config
from opi.generation.manifests import render_template
from opi.services.catalog.base import DeploymentManifestContext
from opi.services.registry import get_service
from opi.services.services_enums import ServiceType
from tests.e2e.helpers import cluster

if TYPE_CHECKING:
    from opi.services.catalog.vlam.endpoint import VlamEndpoint

#: Het antwoord dat de stub op /v1/models geeft. Dezelfde vorm als VLAM: een `data`-lijst,
#: want dat is waar de probe in het e2e-allservices-image op oordeelt.
STUB_MODEL_ID = "vlam-stub"
STUB_MODELS_BODY = json.dumps({"data": [{"id": STUB_MODEL_ID, "object": "model"}], "object": "list"})

#: De naam van de inkomende regel, gelijk aan die in het echte vlam-wt8-projectbestand.
_INBOUND_RULE_NAME = "iedereen-in-het-cluster"


def _haproxy_config(port: int) -> str:
    """De hele stub: een poort, een gezondheidspad, een vast antwoord, en 404 voor de rest."""
    return f"""\
global
    log stdout format raw local0
defaults
    mode http
    log global
    option httplog
    timeout connect 5s
    timeout client 30s
    timeout server 30s
frontend vlam_stub
    bind :{port}
    monitor-uri /healthz
    http-request return status 200 content-type application/json string '{STUB_MODELS_BODY}' if {{ path /v1/models }}
    http-request return status 404 content-type application/json string '{{"error":"not a stubbed path"}}'
"""


def _manifests(endpoint: VlamEndpoint, config: dict) -> str:
    """Namespace, config, workload, service en de dichte deur -- alles behalve de open regel."""
    app = endpoint.pod_labels["app"]
    namespace = endpoint.namespace
    deployment = config["deployment"]
    project = config["project"]
    haproxy_config = _haproxy_config(endpoint.port)
    indented = "\n".join(f"    {line}" for line in haproxy_config.splitlines())
    return f"""\
apiVersion: v1
kind: Namespace
metadata:
  name: "{namespace}"
  labels:
    # Bewust NIET created-by=operations-manager: dit is geen OPI-namespace en hij hoort
    # niet mee te gaan in een opruiming die op dat label selecteert.
    created-by: e2e-vlam-stub
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: "{app}-config"
  namespace: "{namespace}"
data:
  haproxy.cfg: |
{indented}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: "{app}"
  namespace: "{namespace}"
spec:
  replicas: 1
  selector:
    matchLabels:
      app: "{app}"
  template:
    metadata:
      labels:
        # Dezelfde labels die de deployment-sjabloon van OPI zet: hierop selecteert zowel
        # de uitgaande regel van de afnemer als de inkomende regel van de stub.
        app: "{app}"
        deployment: "{deployment}"
        project: "{project}"
        component: application
    spec:
      securityContext:
        runAsNonRoot: true
        runAsUser: 1001
        runAsGroup: 1001
        fsGroup: 1001
      containers:
        - name: app
          image: docker.io/library/haproxy:lts-alpine
          args: ["-f", "/etc/haproxy/haproxy.cfg"]
          ports:
            - containerPort: {endpoint.port}
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: false
            capabilities:
              drop:
                - ALL
          volumeMounts:
            - name: config
              mountPath: /etc/haproxy
      volumes:
        - name: config
          configMap:
            name: "{app}-config"
---
apiVersion: v1
kind: Service
metadata:
  name: "{app}"
  namespace: "{namespace}"
spec:
  selector:
    app: "{app}"
  ports:
    - name: http
      protocol: TCP
      port: {endpoint.port}
      targetPort: {endpoint.port}
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: "{deployment}-stub-tenant-baseline"
  namespace: "{namespace}"
spec:
  # De tenant-baseline in het klein: eigen deployment en de platformnamespace mogen
  # binnen, verder niemand. Zonder deze regel staat de deur al open en meet de test niets.
  podSelector:
    matchLabels:
      deployment: "{deployment}"
  policyTypes:
    - Ingress
  ingress:
    - from:
        - podSelector:
            matchLabels:
              deployment: "{deployment}"
    - from:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: rig-system
"""


def wildcard_policy(cluster_name: str, endpoint: VlamEndpoint) -> str:
    """De inkomende regel, gerenderd door de cross-domain-access-dienst zelf.

    Dit is met opzet geen met de hand geschreven NetworkPolicy: wat in de sandbox de deur
    opent hoort exact te zijn wat OPI op productie zou schrijven voor dezelfde
    wildcard-regel, anders meet de suite haar eigen YAML.
    """
    config = get_vlam_config(cluster_name)
    assert config is not None, f"cluster '{cluster_name}' kent geen VLAM-endpoint"
    project_data = {
        "name": config["project"],
        "services": [
            {
                "name": ServiceType.CROSS_DOMAIN_ACCESS.value,
                "config": {
                    "inbound": [
                        {
                            "name": _INBOUND_RULE_NAME,
                            "from": {"project": "*"},
                            "to": {"component": config["component"], "port": int(config["port"])},
                        }
                    ]
                },
            }
        ],
    }
    deployment = {"name": config["deployment"], "components": [{"reference": config["component"]}]}
    ctx = DeploymentManifestContext(
        project_name=config["project"],
        project_data=project_data,
        deployment=deployment,
        cluster=cluster_name,
        namespace=endpoint.namespace,
    )
    specs = get_service(ServiceType.CROSS_DOMAIN_ACCESS).contribute_deployment_manifests(ctx)
    assert specs, "de cross-domain-access-dienst leverde geen manifest voor de wildcard-regel"
    return "\n---\n".join(render_template(spec.template_path, spec.values) for spec in specs)


def ensure(cluster_name: str, endpoint: VlamEndpoint) -> None:
    """Zet de stub neer (idempotent) en wacht tot hij zelf antwoordt.

    De laatste stap is geen formaliteit: pas als de stub via een port-forward zijn eigen
    /healthz teruggeeft, is een mislukte aanroep VANUIT een afnemer een uitspraak over het
    netwerkpad en niet over een haproxy die zijn configuratie niet kon lezen.
    """
    config = get_vlam_config(cluster_name)
    assert config is not None, f"cluster '{cluster_name}' kent geen VLAM-endpoint"
    _apply(_manifests(endpoint, config))
    _apply(wildcard_policy(cluster_name, endpoint))

    app = endpoint.pod_labels["app"]
    subprocess.run(
        [
            "kubectl",
            "rollout",
            "status",
            f"deployment/{app}",
            "-n",
            endpoint.namespace,
            "--timeout=180s",
        ],
        capture_output=True,
        text=True,
        timeout=200,
        check=False,
    )
    assert cluster.wait_for(
        lambda: bool(cluster.running_pod_names(endpoint.namespace, app)), timeout=180, interval=3
    ), f"de vlam-stub in {endpoint.namespace} kwam niet draaiend"
    pod = cluster.running_pod_names(endpoint.namespace, app)[0]
    status, _ = cluster.http_get_via_port_forward(endpoint.namespace, pod, endpoint.port, "/healthz", timeout=60.0)
    assert status == 200, f"de vlam-stub antwoordt zelf niet op /healthz (status {status})"


def without_the_open_rule(cluster_name: str, endpoint: VlamEndpoint, call):
    """Haal de wildcard-regel weg, doe ``call()``, en zet hem terug.

    Dit is de meting die uitwijst of dit cluster NetworkPolicies HANDHAAFT. Komt de
    afnemer er zonder de open regel nog steeds doorheen, dan doet de CNI niets met
    NetworkPolicies (kindnet in de sandbox is zo'n geval) en zegt een negatieve
    egress-meting niets. Komt hij er niet doorheen, dan is die open regel aantoonbaar
    wat de deur opent -- en pas dan is "zonder de dienst geen weg" een uitspraak over
    de regel in plaats van over bereikbaarheid.
    """
    policy = yaml.safe_load(wildcard_policy(cluster_name, endpoint))
    name = policy["metadata"]["name"]
    subprocess.run(
        ["kubectl", "delete", "networkpolicy", name, "-n", endpoint.namespace, "--ignore-not-found"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    try:
        return call()
    finally:
        _apply(wildcard_policy(cluster_name, endpoint))


def remove(endpoint: VlamEndpoint) -> None:
    """Haal de stub weg. Nooit hard: opruiming mag een test niet laten falen."""
    subprocess.run(
        ["kubectl", "delete", "namespace", endpoint.namespace, "--ignore-not-found", "--wait=false"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _apply(manifests: str) -> None:
    result = subprocess.run(
        ["kubectl", "apply", "-f", "-"],
        input=manifests,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, f"kubectl apply mislukt: {result.stderr.strip()}\n{manifests}"
