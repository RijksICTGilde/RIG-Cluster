"""Helpers to measure the SeaweedFS requirements table (RC-331).

Sets up: `weed_shell` in the SeaweedFS pod, a client pod in a tenant namespace,
and a weed pod in rig-system (without the OPI label, so it doesn't intercept
the operations-manager Service endpoint).
"""

from __future__ import annotations

import json
import shlex
import subprocess
from dataclasses import dataclass

from tests.e2e.helpers import cluster

#: Waar het component draait, en waar het van buiten heet.
NAMESPACE = "rig-system"
SERVER_APP = "seaweedfs"
S3_ENDPOINT = "http://seaweedfs.rig-system.svc.cluster.local:8333"
METRICS_URL = "http://seaweedfs.rig-system.svc.cluster.local:9327/metrics"

#: De twee adressen die `weed shell` meekrijgt. Binnen de pod 127.0.0.1 en niet
#: `localhost`: weed bindt per adres (127.0.0.1 en het pod-IP, zie `netstat` in rij G16) en
#: er is geen IPv6-listener, dus `localhost` komt op ::1 uit en krijgt een refusal. De vraag
#: WELKE van de twee adressen een commando echt nodig heeft is rij G18.
FILER_IN_POD = "127.0.0.1:8888"
MASTER_IN_POD = "127.0.0.1:9333"

#: Het adres van buiten de pod, zoals OPI het zou gebruiken.
FILER_SERVICE = "seaweedfs.rig-system.svc.cluster.local:8888"
MASTER_SERVICE = "seaweedfs.rig-system.svc.cluster.local:9333"

#: De namespace waarin de clientpod staat, met het label dat de NetworkPolicy als tenant
#: herkent.
TENANT_NAMESPACE = "rc331-tenant"
CLIENT_POD = "rc331-s3-client"
AWS_CONTAINER = "aws"
MC_CONTAINER = "mc"

#: De pod die `weed shell` van buiten de SeaweedFS-pod draait. Draagt met opzet NIET het
#: label van OPI (zie de kop), en komt daarom NIET door de NetworkPolicy op de gRPC-poorten:
#: die meting is juist wat rij I4 aantoont.
WEED_POD = "rc331-weed-shell"

#: Het label waar de NetworkPolicy de beheerder aan herkent, en de echte pod die het draagt.
OPI_APP = "operations-manager"

#: Op een vaste versie, zodat een meting herhaalbaar is. Beide staan al in de
#: image-cache van de sandboxnode.
AWS_IMAGE = "amazon/aws-cli:2.31.11"
MC_IMAGE = "ghcr.io/minbzk/base-images/rig-backup:latest"
SEAWEEDFS_IMAGE = "chrislusf/seaweedfs:4.48@sha256:4e61d15fd35994cb1e43e1e553dff106794841fd9a99ade2fc8c8bfce4d7872d"

#: Waar de sleutels in de clientpod landen. Per identiteit een eigen profiel in het
#: aws-credentials-bestand en een eigen MC_HOST-regel.
_AWS_CREDS = "/root/.aws/credentials"
_AWS_CONFIG = "/root/.aws/config"
_MC_ENV = "/tmp/mc-env"

#: Wat er tot nu toe in de clientpod staat. Het bestand wordt elke keer in zijn geheel
#: herschreven, dus zonder dit wist een tweede aanroep de profielen van de eerste.
_PROFIELEN: dict[str, tuple[str, str]] = {}


@dataclass(frozen=True)
class Uitvoer:
    """Wat een gemeten aanroep opleverde."""

    stdout: str
    stderr: str
    code: int

    @property
    def ok(self) -> bool:
        return self.code == 0

    @property
    def tekst(self) -> str:
        """stdout en stderr samen, voor een zoekopdracht op een foutmelding."""
        return f"{self.stdout}\n{self.stderr}".strip()

    def rapport(self, grens: int = 600) -> str:
        """De ruwe uitvoer zoals die in het rapport komt."""
        regels = [f"exit {self.code}"]
        if self.stdout:
            regels.append(f"stdout: {self.stdout[:grens]}")
        if self.stderr:
            regels.append(f"stderr: {self.stderr[:grens]}")
        return "\n".join(regels)

    def json(self) -> dict | list:
        """stdout als JSON. Een lege uitvoer is een leeg object, geen fout."""
        return json.loads(self.stdout) if self.stdout.strip() else {}


def _kubectl(args: list[str], *, stdin: str | None = None, timeout: float = 180.0) -> Uitvoer:
    try:
        klaar = subprocess.run(
            ["kubectl", *args],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        # Zonder argv in de melding: pytest schrijft de tekst van een uitzondering in het
        # verslag, en daar kan een sleutel in staan.
        return Uitvoer("", f"kubectl {' '.join(args[:3])} haalde {timeout:.0f}s niet", 124)
    return Uitvoer(klaar.stdout.strip(), klaar.stderr.strip(), klaar.returncode)


def apply(manifest: str) -> None:
    """``kubectl apply -f -``, met het manifest op stdin."""
    uitvoer = _kubectl(["apply", "-f", "-"], stdin=manifest)
    assert uitvoer.ok, f"kubectl apply mislukte: {uitvoer.stderr}\n{manifest}"


def verwijder(args: list[str]) -> None:
    """Opruimen mag een meting nooit laten falen."""
    _kubectl(["delete", *args, "--ignore-not-found", "--wait=false"], timeout=60.0)


def server_pod() -> str:
    """De naam van de draaiende SeaweedFS-pod."""
    namen = cluster.running_pod_names(NAMESPACE, SERVER_APP)
    assert namen, f"geen draaiende pod met prefix '{SERVER_APP}' in {NAMESPACE}"
    return namen[0]


def wacht_op_server(*, timeout: float = 240.0) -> bool:
    """Wacht tot de Deployment uitgerold is EN de pod Ready staat."""
    _kubectl(
        ["rollout", "status", f"deployment/{SERVER_APP}", "-n", NAMESPACE, f"--timeout={int(timeout)}s"],
        timeout=timeout + 20,
    )
    return cluster.wait_for(
        lambda: (
            _kubectl(
                [
                    "get",
                    "pod",
                    "-n",
                    NAMESPACE,
                    "-l",
                    f"app={SERVER_APP}",
                    "-o",
                    "jsonpath={.items[*].status.conditions[?(@.type=='Ready')].status}",
                ]
            ).stdout.strip()
            == "True"
        ),
        timeout=timeout,
        interval=3,
    )


def exec_in(
    namespace: str,
    pod: str,
    script: str,
    *,
    container: str | None = None,
    stdin: str | None = None,
    timeout: float = 180.0,
) -> Uitvoer:
    """Draai een shell-snipper in een container en geef zijn uitvoer terug."""
    args = ["exec"]
    if stdin is not None:
        args.append("-i")
    args += [pod, "-n", namespace]
    if container:
        args += ["-c", container]
    args += ["--", "sh", "-c", script]
    return _kubectl(args, stdin=stdin, timeout=timeout)


def admin_sleutels() -> tuple[str, str]:
    """Het admin-sleutelpaar uit het secret, zoals de S3-gateway het leest."""
    waarden = cluster.secret_values(NAMESPACE, "seaweedfs-credentials")
    return waarden["AWS_ACCESS_KEY_ID"], waarden["AWS_SECRET_ACCESS_KEY"]


def weed_shell(
    commando: str,
    *,
    filer: str = FILER_IN_POD,
    master: str = MASTER_IN_POD,
    pod: str | None = None,
    namespace: str = NAMESPACE,
    container: str | None = None,
    jwt: bool = True,
) -> Uitvoer:
    """Eén `weed shell`-commando, via stdin, in de SeaweedFS-pod (of een andere pod).

    Eén commando per aanroep: in een gepijpte run schrijft `weed shell` elke fout naar
    stderr en geeft het proces exit 1, en met één commando per proces is die exitcode
    ondubbelzinnig van dit commando.

    ``jwt=False`` haalt WEED_JWT_FILER_SIGNING_KEY uit de omgeving van het commando; dat is
    de meting van rij I5.
    """
    aanroep = f"weed shell -filer={shlex.quote(filer)}"
    if master:
        aanroep += f" -master={shlex.quote(master)}"
    script = aanroep if jwt else f"unset WEED_JWT_FILER_SIGNING_KEY; {aanroep}"
    return exec_in(namespace, pod or server_pod(), script, container=container, stdin=f"{commando}\n")


def _namespace_manifest() -> str:
    return f"""
apiVersion: v1
kind: Namespace
metadata:
  name: {TENANT_NAMESPACE}
  labels:
    # Het label waar de NetworkPolicy van seaweedfs een tenant aan herkent.
    created-by: operations-manager
"""


def _client_manifest() -> str:
    """De clientpod: `aws` en `mc` naast elkaar in een tenant-namespace."""
    return f"""
apiVersion: v1
kind: Pod
metadata:
  name: {CLIENT_POD}
  namespace: {TENANT_NAMESPACE}
  labels:
    app: {CLIENT_POD}
spec:
  restartPolicy: Never
  containers:
  - name: {AWS_CONTAINER}
    image: {AWS_IMAGE}
    command: ["sleep", "5400"]
    resources:
      requests:
        cpu: 10m
        memory: 64Mi
      limits:
        memory: 512Mi
  - name: {MC_CONTAINER}
    image: {MC_IMAGE}
    command: ["sleep", "5400"]
    resources:
      requests:
        cpu: 10m
        memory: 64Mi
      limits:
        memory: 512Mi
"""


def _weed_pod_manifest() -> str:
    """Een pod met de `weed`-binary in rig-system, met zijn EIGEN label (zie de kop)."""
    return f"""
apiVersion: v1
kind: Pod
metadata:
  name: {WEED_POD}
  namespace: {NAMESPACE}
  labels:
    app: {WEED_POD}
spec:
  restartPolicy: Never
  containers:
  - name: weed
    image: {SEAWEEDFS_IMAGE}
    command: ["sleep", "5400"]
    envFrom:
    - secretRef:
        name: seaweedfs-credentials
    resources:
      requests:
        cpu: 10m
        memory: 64Mi
      limits:
        memory: 512Mi
"""


def _wacht_op_pod(namespace: str, naam: str, *, timeout: float = 240.0) -> None:
    uitvoer = _kubectl(
        ["wait", "--for=condition=Ready", f"pod/{naam}", "-n", namespace, f"--timeout={int(timeout)}s"],
        timeout=timeout + 20,
    )
    assert uitvoer.ok, f"pod {namespace}/{naam} kwam niet Ready: {uitvoer.tekst}"


def zet_clientpod_neer() -> None:
    # Eerst wachten tot een VORIGE namespace echt weg is. `ruim_op` verwijdert met
    # --wait=false, dus een tweede gang binnen een paar minuten landt in een namespace die
    # nog Terminating is; kubectl neemt het manifest dan aan en de pod komt nooit. Dat
    # kostte twee hele meetgangen.
    assert cluster.wait_for(
        lambda: (
            _kubectl(["get", "namespace", TENANT_NAMESPACE, "-o", "jsonpath={.status.phase}"]).stdout.strip()
            != "Terminating"
        ),
        timeout=180,
        interval=3,
    ), f"namespace {TENANT_NAMESPACE} bleef Terminating"
    # De namespace eerst, en wachten tot zijn `default`-serviceaccount er is: een pod in
    # een namespace zonder die serviceaccount wordt geweigerd, en op dit cluster loopt de
    # controller-manager achter.
    apply(_namespace_manifest())
    assert cluster.wait_for(
        lambda: _kubectl(["get", "serviceaccount", "default", "-n", TENANT_NAMESPACE]).ok,
        timeout=120,
        interval=2,
    ), f"de serviceaccount 'default' in {TENANT_NAMESPACE} kwam niet"
    apply(_client_manifest())
    _wacht_op_pod(TENANT_NAMESPACE, CLIENT_POD)
    # Path-style adressering en de regio als vaste configuratie, zodat elke aanroep in deze
    # meting dezelfde vorm heeft als wat MinIOSecret een applicatie meegeeft.
    exec_in(
        TENANT_NAMESPACE,
        CLIENT_POD,
        f"mkdir -p /root/.aws && cat > {_AWS_CONFIG}",
        container=AWS_CONTAINER,
        stdin="[default]\nregion = us-east-1\ns3 =\n  addressing_style = path\n",
    )


def zet_weedpod_neer() -> None:
    apply(_weed_pod_manifest())
    _wacht_op_pod(NAMESPACE, WEED_POD)


def ruim_op() -> None:
    verwijder(["namespace", TENANT_NAMESPACE])
    verwijder(["pod", WEED_POD, "-n", NAMESPACE])


def schrijf_sleutels(profielen: dict[str, tuple[str, str]]) -> None:
    """Zet alle sleutelparen in de clientpod, via stdin en nooit via argv.

    ``profielen`` is ``{naam: (access_key, secret_key)}`` en komt BOVENOP wat er al staat.
    Voor `aws` wordt het een credentials-bestand met een profiel per naam, voor `mc` een
    bestand met een ``MC_HOST_<naam>``-regel per naam; beide gaan over stdin naar `cat`.
    """
    _PROFIELEN.update(profielen)
    creds = "".join(
        f"[{naam}]\naws_access_key_id = {ak}\naws_secret_access_key = {sk}\n" for naam, (ak, sk) in _PROFIELEN.items()
    )
    exec_in(
        TENANT_NAMESPACE, CLIENT_POD, f"mkdir -p /root/.aws && cat > {_AWS_CREDS}", container=AWS_CONTAINER, stdin=creds
    )

    host = S3_ENDPOINT.removeprefix("http://")
    mc_env = "".join(f"export MC_HOST_{naam}=http://{ak}:{sk}@{host}\n" for naam, (ak, sk) in _PROFIELEN.items())
    exec_in(TENANT_NAMESPACE, CLIENT_POD, f"cat > {_MC_ENV}", container=MC_CONTAINER, stdin=mc_env)


def aws(profiel: str, script: str, *, timeout: float = 180.0) -> Uitvoer:
    """Een `aws`-aanroep met de sleutels van ``profiel`` uit het credentials-bestand."""
    omgeving = (
        "export AWS_EC2_METADATA_DISABLED=1 "
        f"AWS_ENDPOINT_URL={S3_ENDPOINT} AWS_DEFAULT_REGION=us-east-1 AWS_PROFILE={profiel}; "
    )
    return exec_in(TENANT_NAMESPACE, CLIENT_POD, omgeving + script, container=AWS_CONTAINER, timeout=timeout)


def aws_anoniem(script: str) -> Uitvoer:
    """Dezelfde aanroep zonder enige identiteit."""
    omgeving = (
        "export AWS_EC2_METADATA_DISABLED=1 "
        f"AWS_ENDPOINT_URL={S3_ENDPOINT} AWS_DEFAULT_REGION=us-east-1; unset AWS_PROFILE; "
    )
    return exec_in(TENANT_NAMESPACE, CLIENT_POD, omgeving + script, container=AWS_CONTAINER)


def mc(script: str, *, timeout: float = 300.0) -> Uitvoer:
    """Een `mc`-aanroep in de container met het rig-backup-image."""
    return exec_in(TENANT_NAMESPACE, CLIENT_POD, f". {_MC_ENV}; {script}", container=MC_CONTAINER, timeout=timeout)


def opi_pod() -> str:
    """De echte OPI-pod. De enige pod die de NetworkPolicy op de gRPC-poorten toelaat."""
    namen = cluster.running_pod_names(NAMESPACE, OPI_APP)
    assert namen, f"geen draaiende pod met prefix '{OPI_APP}' in {NAMESPACE}"
    return namen[0]


def connector_in_opi_pod(aanroep: str, *, timeout: float = 120.0) -> Uitvoer:
    """Draai een methode van `SeaweedfsConnector` IN de OPI-pod.

    De echte beheerweg van rij G18: dezelfde code, dezelfde `weed`-binary uit het
    OPI-image en dezelfde settings als provisioning. ``aanroep`` is een methode-aanroep,
    bijvoorbeeld ``list_buckets()``.
    """
    script = (
        "cd /app && PYTHONPATH=/app python3 -c "
        '"import asyncio;'
        "from opi.connectors.seaweedfs_shell import create_seaweedfs_connector;"
        f'print(asyncio.run(create_seaweedfs_connector().{aanroep}))" 2>&1'
    )
    return exec_in(NAMESPACE, opi_pod(), script, timeout=timeout)


def poort_bereikt_uit(namespace: str, pod: str, host: str, poort: int, *, container: str | None = None) -> Uitvoer:
    """Of ``host:poort`` antwoordt vanuit die pod, met een korte time-out.

    Via python: zowel het OPI-image als het rig-backup-image is python-slim en heeft geen
    netcat. Een time-out IS hier het antwoord, dus hij moet kort zijn.
    """
    script = (
        'python3 -c "import socket;socket.create_connection(('
        f"'{host}',{poort}),4).close();print('BEREIKT')\" 2>/dev/null || echo GEEN-ANTWOORD"
    )
    return exec_in(namespace, pod, script, container=container, timeout=60.0)


def netwerk_bereikt(host: str, poort: int) -> Uitvoer:
    """Of een poort vanuit de clientpod antwoordt: de tenantkant van rij I4."""
    return poort_bereikt_uit(TENANT_NAMESPACE, CLIENT_POD, host, poort, container=MC_CONTAINER)
