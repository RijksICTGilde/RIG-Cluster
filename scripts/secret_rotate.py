"""Wachtwoordrotatie: de categorie bepaalt de stappen, het component alleen de plaats.

De module naast `secret_edit.py`. Die module dekt de BESTANDSkant (template, veld voor
veld, SOPS); deze module dekt de APP- en SYNCKant ervoor, want dat is wat rotatie anders maakt
dan bewerken: een wachtwoord dat in git wijzigt maar niet in de applicatie is gewoon drift.

Elk component valt in één categorie, en de categorie bezit de stapvolgorde:

- ``cnpg-secret`` -- het wachtwoord hoort bij een CNPG managed role of de superuser; de
  operator reconciliet de rol uit het secret. Volgorde: bestand eerst, daarna Argo-sync
  afwachten, daarna pas kijken of de operator de rol heeft omgezet (oude login faalt, nieuwe
  werkt), dán pas de consumer herstarten. Handmatig een `ALTER USER` doen is een race: de
  operator zet hem terug naar het secret.
- ``live-app``     -- de app kent geen declaratief pad: redis (ACL SETUSER), de relay
  (management-API), keycloak-admin (kcadm). Daar geldt wél app-eerst: wijzig met de huidige
  waarde, verifieer de nieuwe, schrijf dan het bestand en laat de sync convergeren.
- ``env-restart``  -- de app leest het secret alleen bij start (minio, pgadmin,
  prometheus-token): bestand, sync afwachten, herstart, verifiëren.
- ``external``     -- de wijziging loopt BUITEN dit cluster (TransIP-console, de
  Grafana-token van ODCN): de tool toont de stap, de gebruiker voert hem uit, de tool
  verifieert via de API en schrijft dan pas het bestand.

Drie gates maken de volgorde aantoonbaar in plaats van aangenomen:

1. na de git-stap refresht en synct de tool de eigenaar-Applicatie ZELF (annotatie
   `argocd.argoproj.io/refresh=hard` plus een operation-patch), leest de eindtoestand uit
   `.status.operationState.phase` en toont hem; alleen een Succeeded telt;
2. daarna pollt de tool het cluster-secret tot het de nieuwe waarde draagt (met timeout) --
   herstarten van een consumer gebeurt pas daarna;
3. bij cnpg-componenten komt daar de operator-gate bovenop: psql bewijst dat de nieuwe waarde
   werkt én de oude faalt, vóór één consumer ook maar herstart wordt.

Elke cluster-stap is een ``kubectl``-call via `Kube` (exec in de pod van de component --
psql, redis-cli, kcadm zitten daar; de MinIO-verificatie draait in de OPI-pod want `mc` zit
alleen daar). Niets hiervan draait zonder ``--apply``; zonder die vlag toont elke stap welke
commando's er zouden lopen. Alleen de drift meten: ``--check``.
"""

from __future__ import annotations

import base64
import json
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ruamel.yaml import YAML

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

from key_rotation import public_key_of, read_key, sops_recipients  # type: ignore[reportMissingImports]
from secret_edit import (  # type: ignore[reportMissingImports]
    REPO,
    Cluster,
    EditFailed,
    apply,
    decrypt,
    encrypt,
    encrypted_name,
    fields_of,
    generate,
    key_entry_for,
    templates,
    values_of,
)
from secret_edit import TREES as EDIT_TREES  # type: ignore[reportMissingImports]

# ---------------------------------------------------------------------------
# kubectl-omhulsel. Alles wat het cluster aanraakt gaat hierdoor, zodat tests en
# --dry-run één plek hebben om in te grijpen.
# ---------------------------------------------------------------------------


class Kube:
    """kubectl-aanroepen voor één cluster, met een dry-run die niets uitvoert."""

    def __init__(self, namespace: str, *, dry_run: bool = False, context: str | None = None) -> None:
        self.namespace = namespace
        self.dry_run = dry_run
        self.context = context

    def run(
        self, args: list[str], *, check: bool = True, input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        argv = ["kubectl", *(["--context", self.context] if self.context else []), *args]
        shown = " ".join(shlex.quote(self._redact(a)) for a in argv)
        if self.dry_run:
            print(f"    [dry-run] {shown}")
            return subprocess.CompletedProcess(argv, 0, "", "")
        process = subprocess.run(  # noqa: S603
            argv,
            input=input_text,
            capture_output=True,
            text=True,
            check=False,
        )
        if check and process.returncode != 0:
            raise EditFailed(f"`{shown}` faalde: {process.stderr.strip()[:200]}")
        return process

    @staticmethod
    def _redact(arg: str) -> str:
        """Wachtwoorden horen niet in een foutmelding of scrollback. Alles dat als
        omgevingspaar doorgaat (W=, PGPASSWORD=, REDISCLI_AUTH=, T=, A=) wordt vervangen
        door zijn naam; de structuur van het commando blijft leesbaar."""
        if re.match(r"^(W|P|PGPASSWORD|REDISCLI_AUTH|T|A|N)=", arg):
            return arg.split("=", 1)[0] + "=<weggehaald>"
        return arg

    def exec(
        self, workload: str, command: list[str], *, check: bool = True, namespace: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Voer `command` uit in `workload` (bijv. "deploy/minio" of "rig-db-1")."""
        return self.run(["exec", "-n", namespace or self.namespace, workload, "--", *command], check=check)

    def secret_value(self, name: str, key: str) -> str | None:
        """Eén veld uit een cluster-secret, ontsleuteld. None als het er niet is."""
        result = self.run(
            ["get", "secret", name, "-n", self.namespace, "-o", f"jsonpath={{.data.{key}}}"],
            check=False,
        )
        if result.returncode != 0 or not result.stdout:
            return None
        return base64.b64decode(result.stdout).decode()

    def rollout_restart(self, workload: str, *, namespace: str | None = None) -> None:
        target = namespace or self.namespace
        self.run(["rollout", "restart", "-n", target, workload])
        self.run(["rollout", "status", "-n", target, workload, "--timeout=180s"])


# ---------------------------------------------------------------------------
# De handler-tabel
# ---------------------------------------------------------------------------

# Waar de relay (Stalwart) per cluster te vinden is: de management-API. De odcn-overlay
# zet de host om naar rig-prd-ron; zie de keycloak-deployment (ZAD_MAIL_RELAY_HOST).
RELAY_API = {
    "odcn-production": "http://rig-mail-relay.rig-prd-ron.svc.cluster.local:8080",
    "sandboxed-local": "http://rig-mail-relay.rig-ron.svc.cluster.local:8080",
    "local": "http://rig-mail-relay.rig-ron.svc.cluster.local:8080",
}

# De backup-MinIO: zijn secret is bootstrap (geen Argo-eigenaar, gemeten), en zijn
# namespace wisselt per cluster.
BACKUP_NS = {
    "odcn-production": "rig-prd-backup",
    "sandboxed-local": "rig-backup-destination",
    "local": "rig-backup-destination",
}


def backup_ns_for(cluster_name: str) -> str:
    """De namespace van de backup-MinIO op dit cluster, met een leesbare fout boven een KeyError."""
    try:
        return BACKUP_NS[cluster_name]
    except KeyError:
        raise EditFailed(
            f"geen backup-namespace bekend voor cluster '{cluster_name}' (bekend: {', '.join(sorted(BACKUP_NS))})"
        ) from None


OPI_DEPLOYMENT = "deploy/operations-manager"


def relay_api_for(cluster_name: str) -> str:
    """De management-API van de relay voor dit cluster, met een leesbare fout boven een KeyError."""
    try:
        return RELAY_API[cluster_name]
    except KeyError:
        raise EditFailed(
            f"geen relay-API bekend voor cluster '{cluster_name}' (bekend: {', '.join(sorted(RELAY_API))})"
        ) from None


@dataclass(frozen=True)
class Component:
    """Eén roteerbaar geheim: wat het is, welke categorie stappen erop lossen, welke
    consumers daarna herstart moeten worden."""

    key: str
    title: str
    template: str  # bestandsnaam in infrastructure/.../secrets/templates/
    category: str  # "cnpg-secret", "live-app", "env-restart", "external"
    rotate_fields: tuple[str, ...]  # welke velden van de template meedraaien
    database: str = ""  # cnpg-secret: de database waarin de rol leeft
    db_user_fallback: str = ""  # cnpg-secret: als het bestand geen username-veld heeft
    workloads: tuple[str, ...] = ()  # workloads die na de sync-gate herstart worden
    zad_env: tuple[str, ...] = ()  # env-variabelen in .env-<cluster>.secrets die dezelfde waarde dragen
    gen_without_template: tuple[str, int] = ()  # (kind, lengte) generatie-hint voor rijen zonder template
    note: str = ""


# Een workload-prefiks "relayns:" betekent: de namespace is de relay-namespace van dit
# cluster, op te halen uit RELAY_API. Alles zonder prefiks staat in de namespace van OPI.


# De volgorde van deze lijst IS de rotatievolgorde van --all: eerst de basis waar niets
# van afhangt (database-superuser), dan de declaratieve rollen (cnpg), dan de apps die
# zelf hun wachtwoord krijgen (live-app), dan de restart-gebaseerden, dan de externe;
# keycloak-admin als allerlaatste: dat is het enige wachtwoord waarmee je jezelf buitensluit
# uit het systeem waarmee je het zou herstellen. Herstarten van consumers loopt via
# `workloads` en zit IN de flow (na de sync-gate), niet erachteraan als losse vraag.
COMPONENTS: list[Component] = [
    Component(
        key="postgresql",
        title="PostgreSQL superuser (CNPG rig-db)",
        template="postgres-admin-secret.yaml",
        category="live-app",
        rotate_fields=("password",),
        database="postgres",
        db_user_fallback="postgres",
        workloads=(OPI_DEPLOYMENT,),  # ZAD leest hem als DATABASE_ADMIN_PASSWORD via secretKeyRef
        note="Gemeten op de sandbox (28-09-26): de operator reconciliet de superuser NIET "
        "zelfstandig uit superuserSecret binnen ons venster; daarom loopt dit component als "
        "live-app via ALTER USER, niet via de cnpg-poort van de managed roles.",
    ),
    Component(
        key="keycloak-db",
        title="Databaserol keycloak (CNPG managed role)",
        template="keycloak-db-credentials-secret.yaml",
        category="cnpg-secret",
        rotate_fields=("password",),
        database="keycloak",
        db_user_fallback="keycloak",
        workloads=("deploy/keycloak",),
        note="De eerste rotatie op odcn migreert tevens de overlay-vorm: het bestand ligt daar "
        "nog als platte template-kopie in plaats van versleuteld; de run schrijft dan de canonieke "
        "SOPS-variant en legt de bedrading om. Keycloak valt kort uit tijdens zijn herstart; het "
        "database-wachtwoord zelf gaat zonder downtime (de operator past de rol aan).",
    ),
    Component(
        key="mail-db",
        title="Databaserol mailrelay (CNPG managed role)",
        template="mail-db-credentials-secret.yaml",
        category="cnpg-secret",
        rotate_fields=("password",),
        database="mailrelay",
        db_user_fallback="mailrelay",
        workloads=("relayns:deploy/rig-mail-relay",),
    ),
    Component(
        key="forgejo-db",
        title="Databaserol forgejo (CNPG managed role)",
        template="forgejo-db-credentials-secret.yaml",
        category="cnpg-secret",
        rotate_fields=("password",),
        database="forgejo",
        db_user_fallback="forgejo",
        workloads=("statefulset/forgejo",),
    ),
    Component(
        key="redis",
        title="Redis (rig-redis)",
        template="redis-admin-secret.yaml",
        category="live-app",
        rotate_fields=("REDIS_PASSWORD",),
        workloads=("deploy/rig-redis", OPI_DEPLOYMENT),  # redis schrijft de ACL uit het secret bij start
        note="De redis-pod schrijft de ACL uit het secret heruit bij elke start (init-aclfile); "
        "daarom hoort zijn herstart erbij: dan is de bootpad gelijk aan wat je net roteerde.",
    ),
    Component(
        key="mail-relay",
        title="Mailrelay admin-account (fallback-admin)",
        template="mail-relay-secret.yaml",
        category="env-restart",
        rotate_fields=("MAIL_RELAY_ADMIN_PASSWORD",),
        workloads=("relayns:deploy/rig-mail-relay", OPI_DEPLOYMENT),  # eerst de relay, dan ZAD
        note="Gemeten op de sandbox (28-09): de relay-fallback-admin is GEEN Stalwart-principal "
        "-- /api/principal/<admin> geeft notFound (met HTTP 200 eromheen, dat is Stalwart). Zijn "
        "secret leeft in de config-env bij start, dus dit is een env-restart-rotatie: bestand, "
        "sync, relay-herstart, dan pas ZAD. De auth-proef gebruikt 401/200, nooit de body.",
    ),
    Component(
        key="keycloak-mail",
        title="Keycloak smtp-account op de relay",
        template="keycloak-mail-secret.yaml",
        category="live-app",
        rotate_fields=("smtp-password",),
        workloads=("deploy/keycloak",),
        note="Keycloak leest ZAD_MAIL_RELAY_PASSWORD bij start; de relay zelf kent geen herstart.",
    ),
    Component(
        key="minio",
        title="MinIO root",
        template="minio-admin-secret.yaml",
        category="env-restart",
        rotate_fields=("MINIO_ROOT_PASSWORD",),
        workloads=("deploy/minio", OPI_DEPLOYMENT),
    ),
    Component(
        key="prometheus",
        title="Prometheus metrics-token",
        template="prometheus-metrics-auth-secret.yaml",
        category="env-restart",
        rotate_fields=("token",),
        # De workload heet `prometheus`, niet `prometheus-server`: onder die oude naam sloeg
        # restart_step hem over met een melding, bleef de pod het oude token in zijn env
        # houden, en viel dat niet op omdat dit component geen app-verificatie heeft
        # (gemeten op odcn, 07-10-2026).
        workloads=("deploy/prometheus", OPI_DEPLOYMENT),
        note="ZAD gebruikt dit token via secretKeyRef PROMETHEUS_METRICS_AUTH_TOKEN.",
    ),
    Component(
        key="pgadmin",
        title="pgAdmin inlog",
        template="pgadmin-secret.yaml",
        category="env-restart",
        rotate_fields=("PGLADMIN_DEFAULT_PASSWORD",),
        workloads=("deploy/pgadmin",),
        note="De template zelf kent de typefout PGLADMIN_; die naam volgen, niet verbeteren. "
        "Op odcn is pgAdmin uitgecommentarieerd: alleen de bestandsrotatie doet er dan.",
    ),
    Component(
        key="transip",
        title="TransIP API-sleutel",
        template="transip-secret.yaml",
        category="external",
        rotate_fields=("TRANSIP_ACCOUNT_NAME", "TRANSIP_PRIVATE_KEY"),
        note="De sleutel wijzig je in de TransIP-console; de tool verifieert de nieuwe "
        "via de API en schrijft daarna pas het bestand. external-dns leest hem bij start.",
    ),
    Component(
        key="grafana",
        title="Grafana service-account-token",
        template="",  # leeft alleen in het ZAD-envbestand, niet in een infra-template
        category="external",
        rotate_fields=("GRAFANA_TOKEN",),
        workloads=(OPI_DEPLOYMENT,),
        zad_env=("GRAFANA_TOKEN",),
        note="Token komt van ODCN; geen vraag of ZAD herstart moet, hij is onderdeel van de ronde.",
    ),
    Component(
        key="platform-repo-pat",
        title="Platform repo-PAT (GitHub; OPI schrijft projectmanifests mee)",
        template="",  # leeft in het ZAD-envbestand; de image-default is weg (2026-09-28)
        category="external",
        rotate_fields=("PROJECT_REPO_PASSWORD",),
        workloads=(OPI_DEPLOYMENT,),
        zad_env=("PROJECT_REPO_PASSWORD",),
        note="Aanmaken doe je in de GitHub-console (Settings → Developer settings). De tool "
        "toetst huidige én nieuwe token via de GitHub-API vóór het bestand wordt bijgewerkt. "
        "Daarna zelf: het env-secret regenereren en ZAD herstarten (staat als tekst in de stappen).",
    ),
    Component(
        key="minio-backup",
        title="Backup-MinIO root (backup-destination)",
        template="",  # heeft geen generatie-template; eigen overlay bij backup-destination
        category="bootstrap",
        rotate_fields=("root-password",),
        workloads=(),  # de restart is een eigen stap IN de bootstrap-flow (geen Argo om op te wachten)
        zad_env=("BACKUP_S3_SECRET_KEY",),
        gen_without_template=("random", 20),
        note="Deze namespace staat buiten GitOps, dus zonder Argo-pad: de tool schrijft het "
        "bestand, applicet direct (via stdin, nooit argv), herstart de pod en verifieert met mc. "
        "Wordt op termijn vervangen door een ander backup-systeem.",
    ),
    Component(
        key="keycloak-admin",
        title="Keycloak admin (break-glass)",
        template="keycloak-admin-secret.yaml",
        category="live-app",
        rotate_fields=("KEYCLOAK_ADMIN_PASSWORD",),
        workloads=(OPI_DEPLOYMENT,),  # ZAD leest KEYCLOAK_ADMIN_PASSWORD via secretKeyRef
        note="LAATSTE van elke ronde. De database van Keycloak is leidend; het secret "
        "geldt alleen bij de eerste start - precies waarom dit met de hand misging.",
    ),
]

BY_KEY = {component.key: component for component in COMPONENTS}

# --- Dekking: de tabel en de templates-boom moeten elkaar dekken --------------------------
#
# De onderkant van de redenering is bewust geen annotatie op de secrets maar deze poort:
# bij elke rotatie-ingang en in de tests meet `coverage_gaps()` dat elke template op schijf
# een rij in COMPONENTS heeft (of een benoemde uitsluiting hieronder), en dat elke rij zijn
# templatebestand vindt. Wie een nieuw secret-template toevoegt, wordt dus binnen een run
# gedwongen na te denken over categorie en velden -- dat is de poort tegen "iets missen".

# Templates die bewust GEEN rotatierij hebben (rotatie daar is een andere discipline of is
# nog niet onderzocht). Een nieuwe template die hier niet staat én niet in de tabel, maakt de
# dekking-poort en de toetsen rood: dat is hoe niets stilletjes verdwijnt.
NON_ROTATABLE: dict[str, str] = {
    "vault-init-secret.yaml": "unseal-keys roteren is een eigen procedure, geen wachtwoordwissel",
    "chisel-auth-secret.yaml": "nog niet onderzocht; toevoegen aan de tabel is dan een bewuste stap",
}

# Rijen zonder template-bestand, met de reden (hun schrijfpad is anders dan de templates-boom).
TEMPLATELESS: dict[str, str] = {
    "transip": "de sleutel komt van buiten het cluster; het doelbestand in de overlay staat er wel",
    "grafana": "leeft uitsluitend in het ZAD-envbestand",
    "platform-repo-pat": "leeft uitsluitend in het ZAD-envbestand (image-default is weg sinds 28-09-2026)",
}


def coverage_gaps() -> list[str]:
    """De dekking-poort: templates, tabelrijen en de bewuste uitsluitingen moeten sluiten.

    Een template zonder rij én zonder benoemde reden is een gemis; een rij die naar een
    afwezig templatebestand verwijst eveneens. Deze functie is zijn eigen bewijs: de toets
    draait hem in CI en de rotatie-ingang draait hem vóór elke ronde.
    """
    templates_dir = EDIT_TREES["infrastructure"][0]
    on_disk = {path.name for path in templates(templates_dir)}
    in_table = {component.template for component in COMPONENTS if component.template}
    gaps: list[str] = [
        f"template {name} heeft geen rij in de tabel en staat niet in NON_ROTATABLE"
        for name in sorted(on_disk - in_table - set(NON_ROTATABLE))
    ]
    for name in sorted(in_table - on_disk):
        owner = next(c.key for c in COMPONENTS if c.template == name)
        if owner not in TEMPLATELESS:
            gaps.append(f"rij {owner} wijst naar {name}, maar dat bestand staat niet in {templates_dir}")
    return gaps


def env_file_for(cluster: str) -> Path:
    """Het ZAD-envbestand van een cluster: `.env.<cluster>.secrets` in de OPI-map."""
    return REPO / "operations-manager/python" / f".env.{cluster}.secrets"


def read_env_file(path: Path) -> dict[str, str]:
    """De platte KEY=VALUE-regels van het envbestand, comments en lege regels overgeslagen."""
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def write_env_file(path: Path, updates: dict[str, str]) -> list[str]:
    """Vervang of voeg toe. Geeft de regels terug zoals ze in de samenvatting staan."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    done: list[str] = []
    for name, value in updates.items():
        replaced = False
        for index, line in enumerate(lines):
            if line.strip().startswith(f"{name}="):
                lines[index] = f"{name}={value}"
                replaced = True
                break
        if not replaced:
            lines.append(f"{name}={value}")
        done.append(name)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return done


# ---------------------------------------------------------------------------
# Stappen. Een rotatie is een lijst stappen; --dry-run toont ze, --apply voert ze uit,
# --check voert alleen de fase "check" uit (lees-only: de drift-meting).
# ---------------------------------------------------------------------------


@dataclass
class Step:
    title: str
    phase: str  # check | apply | verify | file | env | followup | manual
    dry_text: list[str]  # wat --dry-run toont (commando's met <HUIDIG>/<NIEUW>)
    run: Callable[[Ctx], str] | None = None  # voert uit en geeft één korte uitkomstregel


@dataclass
class Ctx:
    """Alles wat een stap nodig heeft: het cluster, de waarden, de vraagbaak."""

    cluster: Cluster
    kube: Kube
    current: dict[str, str]  # de huidige waarden uit het bestand (of ingevoerd bij drift)
    new: dict[str, str]
    apply_changes: bool
    rotating: str = ""  # het veld dat roteert
    extra: dict[str, str] = field(default_factory=dict)


def py_quote(value: str) -> str:
    """Veilig als letterlijke string in een python -c snippet (waarden zijn [A-Za-z0-9])."""
    if not value.isascii() or any(c in value for c in "'\"\\\n"):
        raise EditFailed("waarde bevat tekens die niet veilig door een python-one-liner kunnen")
    return f"'{value}'"


def _ok(failure: str) -> None:  # pragma: no cover - hulpje voor leesbaarheid hieronder
    raise EditFailed(failure)


def _accepts(probe: Callable[[], object]) -> bool:
    """Of een auth-proef de meegegeven waarde accepteert.

    De proeven in dit bestand melden een afwijzing met EditFailed, dus een uitzondering
    betekent hier "afgewezen". Bij een oude waarde ná een rotatie is dat precies de
    gewenste uitkomst, en daarom is deze helper de omkering die de aanroepers nodig hebben.
    """
    try:
        probe()
    except EditFailed:
        return False
    return True


def _reject_stale(ctx: Ctx, probe: Callable[[], object], *, label: str) -> str:
    """Faal zolang de OUDE waarde na de rotatie nog geaccepteerd wordt.

    Dit is de tweede helft van het bewijs. Alleen toetsen of de nieuwe waarde werkt liet op
    07-10-2026 een redis-rotatie groen afsluiten terwijl het oude wachtwoord nog gewoon
    inlogde: `>pw` vulde de ACL aan in plaats van te vervangen. De cnpg-gate toetste beide
    richtingen al, de andere componenten niet. In dry-run is er niets gemeten en zegt dit
    dat ook, in plaats van groen te melden wat niet is geprobeerd.
    """
    if ctx.kube.dry_run:
        return "oude waarde niet getoetst (dry-run)"
    if _accepts(probe):
        _ok(
            f"{label}: de OUDE waarde werkt na de rotatie nog steeds, dus de wissel is niet doorgekomen. "
            "Controleer of het mechanisme de waarde VERVANGT in plaats van aanvult."
        )
    return "oude waarde afgewezen"


# --- gedeelde cluster-commando's -----------------------------------------------

APPS_RESOURCE = "applications.argoproj.io"


def argo_owner_of(kube: Kube, secret_name: str) -> tuple[str, str] | None:
    """De ArgoCD-Applicatie die dit Secret beheert, als (naam, app-namespace).

    Twee sporen, want ze verschillen per cluster: het label `app.kubernetes.io/instance`
    (sandbox) en de annotatie `argocd.argoproj.io/tracking-id` (odcn). Geen van beide
    aanwezig betekent: dit secret komt niet uit een GitOps-sync en hoort bij het
    bootstrap-pad -- dat meldt de aanroeper dan expliciet, in plaats van dat de tool
    eindeloos op een sync zou wachten die nooit komt.
    """
    result = kube.run(
        [
            "get",
            "secret",
            secret_name,
            "-n",
            kube.namespace,
            "-o",
            "jsonpath={.metadata.labels.app\\.kubernetes\\.io/instance}{'|'}{.metadata.annotations.argocd\\.argoproj\\.io/tracking-id}",
        ],
        check=False,
    )
    if result.returncode != 0:
        return None
    label, _, tracking = (result.stdout or "").partition("|")
    app = label or tracking.split(":", 1)[0]
    if not app:
        return None
    found = kube.run(
        ["get", APPS_RESOURCE, "-A", "-o", f"jsonpath={{.items[?(@.metadata.name=='{app}')].metadata.namespace}}"],
        check=False,
    )
    if found.returncode != 0 or not found.stdout.strip():
        raise EditFailed(f"Applicatie '{app}' (eigenaar van {secret_name}) niet gevonden in het cluster")
    return app, found.stdout.strip()


def argo_refresh_and_sync(kube: Kube, app: str, app_namespace: str, *, timeout: int = 120) -> str:
    """Refresh én trigger de sync van de Applicatie, en faal VROEG als Argo dat rapporteert.

    Deze functie bewijst bewust géén "gesynct": `.status.operationState.phase` is na een
    patch nog te lang de stand van een eerdere operatie (gemeten: onmiddellijk 'Succeeded'
    terwijl er nog niets landde). Succes bewijst alleen het cluster-secret zélf; dat is de
    taak van `wait_for_secret_value` daarna. Hier blijft: refresh, trigger, en kijk kort of
    de applicatie direct rood gaat, zodat je dat niet pas na een timeout ziet.
    """
    kube.run(["annotate", APPS_RESOURCE, app, "-n", app_namespace, "argocd.argoproj.io/refresh=hard", "--overwrite"])
    patched = kube.run(
        [
            "patch",
            APPS_RESOURCE,
            app,
            "-n",
            app_namespace,
            "--type=merge",
            "-p",
            '{"operation":{"initiatedBy":{"username":"zad-wachtwoord-rotatie"},"sync":{}}}',
        ],
        check=False,
    )
    note = ""
    if patched.returncode != 0:
        if "already" in patched.stderr and "progress" in patched.stderr:
            note = " (de geautomatiseerde sync liep al; die wordt afgewacht)"
        else:
            raise EditFailed(f"sync starten faalde: {patched.stderr.strip()[:200]}")
    deadline = time.monotonic() + timeout
    while True:
        state = kube.run(
            [
                "get",
                APPS_RESOURCE,
                app,
                "-n",
                app_namespace,
                "-o",
                "jsonpath={.status.operationState.phase}{'|'}{.status.operationState.message}{'|'}{.status.sync.status}",
            ],
            check=False,
        )
        phase, _, rest = (state.stdout or "").partition("|")
        message, _, sync_status = rest.partition("|")
        if phase in ("Failed", "Error"):
            raise EditFailed(f"sync van {app} is {phase}: {message.strip()[:200]}")
        if sync_status in ("Synced", "") and phase != "Running":
            # niet 'klaar' maar ook niet faal-op-gang: laat de waargemeugde poll beslissen
            return f"sync op {app} geactiveerd{note}"
        if time.monotonic() > deadline:
            raise EditFailed(f"Argo rapporteerde {timeout}s lang 'Running'; controleer de applicatie: {app}")
        time.sleep(2)


def wait_for_secret_value(kube: Kube, secret_name: str, key: str, expected: str, *, timeout: int = 300) -> str:
    """Wacht tot het cluster-secret de verwachte waarde draagt. Dit is DE gate waar
    restarts op mogen wachten: nooit 'Argo heeft vast wel gesynct'. Falende operaties aan
    de Argo-kant (Fails/Errors) laten zichtbaar via `argo_refresh_and_sync`; hier telt
    alleen de waarde."""
    deadline = time.monotonic() + timeout
    while True:
        live = kube.secret_value(secret_name, key)
        if live == expected:
            return f"cluster-secret {secret_name}.{key} draagt de nieuwe waarde"
        if time.monotonic() > deadline:
            raise EditFailed(
                f"cluster-secret {secret_name} draagt de nieuwe waarde na {timeout}s nog niet "
                f"(live: {'afwezig' if live is None else 'een andere waarde'})"
            )
        time.sleep(2)


def psql_check(kube: Kube, user: str, password: str, database: str = "postgres") -> str:
    """Werkt dit wachtwoord? Eén SELECT over TCP (de pod-socket is trust, TCP is het niet)."""
    result = kube.exec(
        "rig-db-1",
        ["env", f"PGPASSWORD={password}", "psql", "-h", "localhost", "-U", user, "-d", database, "-tAc", "SELECT 1"],
        check=False,
    )
    if result.returncode == 0 and not kube.dry_run:
        return "OK"
    if kube.dry_run:
        return "overgeslagen (dry-run)"
    _ok(f"inloggen op rig-db als {user} met dit wachtwoord faalde")
    return ""


def relay_call(
    kube: Kube, relay_url: str, admin: str, password: str, method: str, path: str, payload: str | None
) -> str:
    """Eén management-call naar de Stalwart-relay, vanuit de OPI-pod (die heeft python3).

    Geeft "HTTP <status>: <korte body>" terug. Stalwart stopt zijn fouten in een 200-body
    (gemeten: {"error":"notFound"} op onbekende principals), dus de check hoort op de body:
    een error erin is áltijd een mislukte call, en 401 eveneens. Alleen zo heet iets gelukt.
    """
    snippet = (
        "import os,urllib.request,urllib.error,json\n"
        "import base64 as b\n"
        f"req=urllib.request.Request(os.environ['U']+{py_quote(path)}, method={py_quote(method)})\n"
        "req.add_header('Authorization','Basic '+b.b64encode((os.environ['A']+':'+os.environ['W']).encode()).decode())\n"
        "req.add_header('Content-Type','application/json')\n"
        "data=os.environ.get('P')\n"
        "try:\n"
        "    r=urllib.request.urlopen(req, data=data.encode() if data else None, timeout=15)\n"
        "    print(f'HTTP {r.status}: '+r.read(200).decode())\n"
        "except urllib.error.HTTPError as e:\n"
        "    print(f'HTTP {e.code}: '+e.read(200).decode())\n"
    )
    return kube.exec(
        OPI_DEPLOYMENT,
        ["env", f"U={relay_url}", f"A={admin}", f"W={password}", f"P={payload or ''}", "python3", "-c", snippet],
    ).stdout.strip()


def relay_ok(outcome: str) -> bool:
    """Een relay_call die een wijziging claimt is pas gelukt als hij niet 401 gaf én er geen
    Stalwart-errorbody terugkwam ({"error": ...} -- die kan in een HTTP 200 zitten)."""
    return "HTTP 401" not in outcome and '"error"' not in outcome


def relay_auth_step(component: Component, cluster: Cluster, *, phase: str, which: str) -> Step:
    """De auth-proef voor de relay-fallback-admin. Strikte regel na de meting van vandaag:
    HTTP 401 betekent fout, elke andere status (ook een 200 met Stalwart-errorbody) betekent
    dat de AUTHENTICATIE werkte. Wij meten inloggen, niet de zin van de endpoint."""

    def _auth(ctx: Ctx, password: str) -> str:
        user = ctx.current.get("MAIL_RELAY_ADMIN_USERNAME", "admin")
        outcome = relay_call(ctx.kube, relay_api_for(cluster.name), user, password, "GET", "/api/principal", None)
        if "HTTP 401" in outcome:
            _ok(f"relay-auth afgewezen: {outcome[:120]}")
        return outcome

    def _probe(ctx: Ctx) -> str:
        if ctx.kube.dry_run:
            return "overgeslagen (dry-run)"
        password = ctx.current[ctx.rotating] if which == "huidige" else ctx.new[ctx.rotating]
        outcome = _auth(ctx, password)
        line = f"relay-auth met {which} waarde OK: {outcome[:80]}"
        if which == "nieuwe":
            line += "; " + _reject_stale(ctx, lambda: _auth(ctx, ctx.current[ctx.rotating]), label="mailrelay-admin")
        return line

    titles = {
        "check": "Huidige waarde testen (relay-auth, fallback-admin)",
        "verify": "Verifiëren (relay-auth): nieuwe waarde werkt, oude is afgewezen",
    }
    return Step(titles[phase], phase, ["GET <relay>/api/*  met basic-auth <waarde>; 401 = mislukt"], _probe)


# --- stapbouwers per component ---------------------------------------------------


def db_user_of(component: Component, current: dict[str, str]) -> str:
    """De rolnaam: uit het gelezen secret, anders de vastgelegde fallback van het component."""
    return current.get("username") or component.db_user_fallback


def cnpg_check_step(component: Component) -> Step:
    """Pre-flight voor een CNPG-rol: huidige waarde uit het bestand moet de app openen.

    Anders is er drift en vraagt `execute()` om het huidige app-wachtwoord -- bij rotatie
    de enige sleutel tot de app.
    """
    return Step(
        f"Huidige waarde testen tegen rig-db (rol {component.db_user_fallback})",
        "check",
        [
            f"kubectl exec rig-db-1 -- env PGPASSWORD=<HUIDIG> psql -h localhost -U {component.db_user_fallback} "
            f"-d {component.database} -tAc 'SELECT 1'"
        ],
        lambda ctx: (
            f"inloggen met huidige waarde: "
            f"{psql_check(ctx.kube, db_user_of(component, ctx.current), ctx.current[ctx.rotating], component.database)}"
        ),
    )


def cnpg_operator_verify_step(component: Component, *, timeout: int = 180) -> Step:
    """De operator-gate: bewijs dat CNPG de rol heeft omgezet naar de gesyncte waarde.

    Pas als psql met de NIEUWE waarde lukt én de OUDE faalt is de doorbraak zeker; voor
    die tijd mag geen consumer herstart worden. Eén rol kan maar één wachtwoord hebben,
    dus 'nieuw werkt + oud faalt' is samen het bewijs dat de reconcile is geland.
    """

    def _verify(ctx: Ctx) -> str:
        user = db_user_of(component, ctx.current)
        new = ctx.new[ctx.rotating]
        deadline = time.monotonic() + timeout
        while True:
            landed = ctx.kube.exec(
                "rig-db-1",
                [
                    "env",
                    f"PGPASSWORD={new}",
                    "psql",
                    "-h",
                    "localhost",
                    "-U",
                    user,
                    "-d",
                    component.database,
                    "-tAc",
                    "SELECT 1",
                ],
                check=False,
            )
            if landed.returncode == 0:
                break
            if ctx.kube.dry_run:
                break
            if time.monotonic() > deadline:
                raise EditFailed(
                    f"de operator heeft de rol {user} na {timeout}s niet op de nieuwe waarde gezet; "
                    "controleer de logs van de CNPG-operator"
                )
            time.sleep(3)
        if ctx.kube.dry_run:
            return "overgeslagen (dry-run)"
        old = ctx.kube.exec(
            "rig-db-1",
            [
                "env",
                f"PGPASSWORD={ctx.current[ctx.rotating]}",
                "psql",
                "-h",
                "localhost",
                "-U",
                user,
                "-d",
                component.database,
                "-tAc",
                "SELECT 1",
            ],
            check=False,
        )
        if old.returncode == 0:
            raise EditFailed(f"de OUDE waarde voor rol {user} werkt nog steeds; de reconcile is niet geland")
        return f"rol {user}: nieuwe waarde werkt, oude faalt — operator-reconcile geland"

    return Step(
        "Operator-reconcile afwachten (oude login faalt, nieuwe werkt)",
        "verify",
        ["psql met <NIEUW> tot OK; daarna psql met <HUIDIG> moet falen"],
        _verify,
    )


def require_safe_sql_literal(value: str) -> None:
    """Waarden die zonder escaping in SQL/ACL-commando's belanden, mogen alfanumeriek
    (plus - en _) zijn -- alles binnen die vorm is per definitie veilig."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise EditFailed("waarde mag alleen letters, cijfers, '-' en '_' bevatten (SQL/ACL-veiligheid)")


def pg_superuser_steps(kube: Kube, values: Ctx) -> list[Step]:
    """De superuser van rig-db: app-eerst, want de operator reconciliet die rol niet
    vanuit superuserSecret (gemeten op de sandbox, 28-09-2026)."""

    def _apply(ctx: Ctx) -> str:
        new = ctx.new[ctx.rotating]
        require_safe_sql_literal(new)
        user = db_user_of(BY_KEY["postgresql"], ctx.current)
        ctx.kube.exec(
            "rig-db-1", ["psql", "-c", f"ALTER USER {user} WITH PASSWORD '{new}';", "-U", "postgres", "-d", "postgres"]
        )
        return f"rol {user} heeft het nieuwe wachtwoord"

    def _verify(ctx: Ctx) -> str:
        user = db_user_of(BY_KEY["postgresql"], ctx.current)
        fresh = psql_check(ctx.kube, user, ctx.new[ctx.rotating], "postgres")
        stale = _reject_stale(
            ctx,
            lambda: psql_check(ctx.kube, user, ctx.current[ctx.rotating], "postgres"),
            label="postgres-superuser",
        )
        return f"inloggen met nieuwe waarde: {fresh}; {stale}"

    return [
        Step(
            "Huidige waarde testen tegen rig-db (rol postgres)",
            "check",
            [
                "kubectl exec rig-db-1 -- env PGPASSWORD=<HUIDIG> psql -h localhost -U postgres -d postgres -tAc 'SELECT 1'"
            ],
            lambda ctx: (
                "inloggen met huidige waarde: "
                + psql_check(
                    ctx.kube, db_user_of(BY_KEY["postgresql"], ctx.current), ctx.current[ctx.rotating], "postgres"
                )
            ),
        ),
        Step(
            "ALTER USER postgres",
            "apply",
            ["kubectl exec rig-db-1 -- psql -c \"ALTER USER postgres WITH PASSWORD '<NIEUW>'\""],
            _apply,
        ),
        Step("Verifiëren met de nieuwe waarde", "verify", [], _verify),
    ]


def redis_steps(kube: Kube, values: Ctx) -> list[Step]:
    def _expect(outcome: str, want: str, ctx: Ctx) -> str:
        """PING/ACL SETUSER-textuitslag. `redis-cli` geeft bij NOAUTH/WRONGPASS/ERR de melding
        op stdout met exitcode 0 (gemeten), dus de returncode zegt niets; de inhoud wel."""
        if want not in outcome and not ctx.kube.dry_run:
            _ok(f"redis antwoordde niet als verwacht ({want!r} vereist): {outcome[:120]!r}")
        return outcome if not ctx.kube.dry_run else "overgeslagen (dry-run)"

    def _apply(ctx: Ctx) -> str:
        new_pw = ctx.new[ctx.rotating]
        require_safe_sql_literal(new_pw)
        # ACL SETUSER leeft in geheugen; ACL SAVE is wat de herstart-persistentie schrijft
        # naar /data/users.acl. Zonder die tweede draait een restart van de pod terug naar de
        # oude waarde (gemeten in de sandbox, 28-09).
        #
        # `resetpass` hoort ervoor: in Redis VOEGT `>pw` een wachtwoord toe aan de gebruiker,
        # het vervangt niets. Zonder resetpass bleef het oude wachtwoord geldig en accepteerde
        # `default` er twee, terwijl de PING met de nieuwe waarde gewoon slaagde. Zo leek de
        # rotatie te werken en was de oude waarde nog bruikbaar (gemeten op odcn, 07-10-2026).
        #
        # Elk commando is een eigen redis-cli-verbinding, dus een eigen aanmelding. Daarom
        # hoort bij elk commando de waarde die op DAT moment geldig is: de SETUSER meldt zich
        # nog met de oude, en vanaf dat commando bestaat die niet meer, dus de SAVE erna moet
        # de nieuwe gebruiken. Beide met de oude gaf NOAUTH op de SAVE, met als uitkomst een
        # nieuw wachtwoord in geheugen dat nergens stond opgeslagen (gemeten op odcn, 07-10-2026).
        for command, auth in (
            (["ACL", "SETUSER", "default", "on", "resetpass", f">{new_pw}"], ctx.current[ctx.rotating]),
            (["ACL", "SAVE"], new_pw),
        ):
            result = ctx.kube.exec(
                "deploy/rig-redis",
                ["env", f"REDISCLI_AUTH={auth}", "redis-cli", "--no-auth-warning", *command],
                check=False,
            )
            outcome = result.stdout.strip()
            if "OK" not in outcome and not ctx.kube.dry_run:
                _ok(
                    f"redis antwoordde niet als verwacht op {' '.join(command[:2])} ('OK' vereist): {outcome[:120]!r}. "
                    "Staat de SETUSER wel en de SAVE niet, dan draagt het geheugen een waarde die niet op schijf "
                    "staat en ook niet in het secret: herstel met `kubectl rollout restart deploy/rig-redis`, "
                    "dan schrijft de init-container de ACL terug uit het secret."
                )
        return "ACL vervangen en opgeslagen (resetpass, SAVE)"

    def _verify(ctx: Ctx) -> str:
        """Bewijst beide helften: de nieuwe waarde werkt en de oude is dood.

        Alleen de eerste helft toetsen is niet genoeg. Dat liet de rotatie van 07-10-2026 op
        odcn groen afsluiten terwijl het oude wachtwoord nog werkte, want `>pw` voegde toe in
        plaats van te vervangen. De cnpg-componenten toetsen beide richtingen al; redis deed
        dat niet.
        """
        result = ctx.kube.exec(
            "deploy/rig-redis",
            ["env", f"REDISCLI_AUTH={ctx.new[ctx.rotating]}", "redis-cli", "--no-auth-warning", "PING"],
        )
        fresh = _expect(result.stdout.strip(), "PONG", ctx)
        stale = ctx.kube.exec(
            "deploy/rig-redis",
            ["env", f"REDISCLI_AUTH={ctx.current[ctx.rotating]}", "redis-cli", "--no-auth-warning", "PING"],
            check=False,
        ).stdout.strip()
        if "PONG" in stale and not ctx.kube.dry_run:
            _ok(f"de OUDE waarde werkt na de rotatie nog steeds ({stale[:60]!r}): de ACL is niet vervangen")
        return f"PING met nieuwe waarde: {fresh}; oude waarde afgewezen"

    return [
        Step(
            "Huidige waarde testen (PING met bestaande waarde)",
            "check",
            ["kubectl exec deploy/rig-redis -- env REDISCLI_AUTH=<HUIDIG> redis-cli --no-auth-warning PING"],
            lambda ctx: (
                "PING met huidige waarde: "
                + _expect(
                    ctx.kube.exec(
                        "deploy/rig-redis",
                        ["env", f"REDISCLI_AUTH={ctx.current[ctx.rotating]}", "redis-cli", "--no-auth-warning", "PING"],
                        check=False,
                    ).stdout.strip(),
                    "PONG",
                    ctx,
                )
            ),
        ),
        Step(
            "ACL SETUSER default",
            "apply",
            [
                "kubectl exec deploy/rig-redis -- env REDISCLI_AUTH=<HUIDIG> redis-cli "
                "ACL SETUSER default on resetpass '><NIEUW>'",
                "daarna ACL SAVE, zodat /data/users.acl en het geheugen hetzelfde zeggen",
            ],
            _apply,
        ),
        Step("Verifiëren: nieuwe waarde werkt, oude is afgewezen", "verify", [], _verify),
    ]


def keycloak_admin_steps(kube: Kube, values: Ctx) -> list[Step]:
    kcadm = "/opt/keycloak/bin/kcadm.sh"
    user = values.current.get("KEYCLOAK_ADMIN", "admin")

    def _login(ctx: Ctx, password: str, tag: str) -> str:
        config = f"/tmp/kc-rot-{tag}"
        result = ctx.kube.exec(
            "deploy/keycloak",
            [
                kcadm,
                "config",
                "credentials",
                "--server",
                "http://localhost:8080",
                "--realm",
                "master",
                "--user",
                user,
                "--password",
                password,
                "--config",
                config,
            ],
            check=False,
        )
        if result.returncode != 0 and not ctx.kube.dry_run:
            _ok(f"kcadm-login ({tag}) faalde: {result.stderr.strip()[:150]}")
        return "OK" if not ctx.kube.dry_run else "overgeslagen (dry-run)"

    def _apply(ctx: Ctx) -> str:
        # Inloggen met de HUIDIGE waarde geeft een geldige sessie; die blijft bestaan
        # als het wachtwoord wijzigt, en is dus ook de terugval: mislukt de verificatie,
        # zet met die sessie de oude waarde terug.
        _login(ctx, ctx.current[ctx.rotating], "werk")
        ctx.kube.exec(
            "deploy/keycloak",
            [
                kcadm,
                "set-password",
                "--config",
                "/tmp/kc-rot-werk",
                "-r",
                "master",
                "--username",
                user,
                "--new-password",
                ctx.new[ctx.rotating],
            ],
        )
        return f"wachtwoord van {user} gezet"

    return [
        Step(
            f"Huidige waarde testen (kcadm-login als {user})",
            "check",
            [
                f"kubectl exec deploy/keycloak -- {kcadm} config credentials --server http://localhost:8080 --realm master --user {user} --password <HUIDIG> --config /tmp/kc-rot-check"
            ],
            lambda ctx: f"kcadm-login met huidige waarde: {_login(ctx, ctx.current[ctx.rotating], 'check')}",
        ),
        Step(
            f"Wachtwoord van {user} zetten via de Admin API",
            "apply",
            [
                f"kubectl exec deploy/keycloak -- {kcadm} set-password -r master --username {user} --new-password <NIEUW>"
            ],
            _apply,
        ),
        Step(
            "Verifiëren: frisse login met de nieuwe waarde",
            "verify",
            [],
            lambda ctx: f"kcadm-login met nieuwe waarde: {_login(ctx, ctx.new[ctx.rotating], 'verify')}",
        ),
    ]


def _mc(kube: Kube, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    """mc draait in de OPI-pod; de MinIO-image heeft hem niet. Eigen config-dir per run,
    zodat er geen alias met een wachtwoord in de pod blijft liggen."""
    return kube.exec(
        OPI_DEPLOYMENT,
        [
            "sh",
            "-c",
            f"mc --config-dir /tmp/mc-rot {' '.join(shlex.quote(a) for a in args)}; rc=$?; rm -rf /tmp/mc-rot; exit $rc",
        ],
        check=check,
    )


def minio_check_step(kube: Kube, values: Ctx, endpoint: str) -> Step:
    """De pre-flight voor MinIO: mc alias met de huidige waarde, vanuit de OPI-pod
    (de MinIO-image heeft geen mc)."""
    user = values.current.get("MINIO_ROOT_USER", "admin")

    return Step(
        "Huidige waarde testen (mc alias vanuit de OPI-pod)",
        "check",
        [f"kubectl exec {OPI_DEPLOYMENT} -- mc alias set rot {endpoint} {user} <HUIDIG> --api s3v4"],
        lambda ctx: f"mc met huidige waarde: {_mc_check(ctx, endpoint, user, ctx.current[ctx.rotating])}",
    )


def _mc_check(ctx: Ctx, endpoint: str, user: str, password: str) -> str:
    result = _mc(ctx.kube, ["alias", "set", "rot", endpoint, user, password, "--api", "s3v4"], check=False)
    if result.returncode != 0 and not ctx.kube.dry_run:
        _ok(f"mc alias met deze waarde faalde: {result.stderr.strip()[:150]}")
    return "OK" if not ctx.kube.dry_run else "overgeslagen (dry-run)"


def bootstrap_component_steps(component: Component, cluster: Cluster, kube: Kube, ctx: Ctx) -> list[Step]:
    """De bootstrap-categorie: het secret staat buiten GitOps, dus de tool schrijft het
    bestand én applicet het direct (via stdin op kubectl apply -- secrets nooit via argv),
    herstart de pod, en verifiëert met de nieuwe waarde via mc."""

    def _deploy_secret_manifest() -> str:
        new = ctx.new[ctx.rotating]
        require_safe_sql_literal(new)
        user = ctx.current.get("BACKUP_S3_ACCESS_KEY", "backup-admin")
        namespace = backup_ns_for(cluster.name)
        manifest = (
            "apiVersion: v1\nkind: Secret\nmetadata:\n"
            f"  name: minio-credentials\n  namespace: {namespace}\n"
            "  labels:\n    app.kubernetes.io/name: minio\n    app.kubernetes.io/component: backup-storage\n"
            "type: Opaque\nstringData:\n"
            f'  root-user: "{user}"\n  root-password: "{new}"\n'
        )
        return manifest

    def overlay_path() -> Path:
        folder = cluster.folders["infrastructure"]
        return (
            REPO
            / "infrastructure/bootstrap/infrastructure/backup-destination/controller/overlays"
            / folder
            / "secret.sops.yaml"
        )

    def _write(ctx2: Ctx) -> str:
        destination = overlay_path()
        fields = {
            "root-user": ctx2.current.get("BACKUP_S3_ACCESS_KEY", "backup-admin"),
            "root-password": ctx2.new[ctx2.rotating],
        }
        plaintext = (
            "apiVersion: v1\nkind: Secret\nmetadata:\n"
            "  name: minio-credentials\n"
            f"  namespace: {backup_ns_for(cluster.name)}\n"
            "  labels:\n    app.kubernetes.io/name: minio\n    app.kubernetes.io/component: backup-storage\n"
            "type: Opaque\nstringData:\n" + "".join(f'  {k}: "{v}"\n' for k, v in fields.items())
        )
        if destination.exists():
            found = sops_recipients(destination)
            entry = key_entry_for(found[0])
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            convention = REPO / SEED_KEYS[cluster.name]
            if not convention.is_file():
                raise EditFailed(f"conventie-sleutel voor {cluster.name} ontbreekt: {convention}")
            private = read_key(convention)
            entry = None
            encrypt(plaintext, destination, private)
            return f"{destination} aangemaakt (sleutel {convention.name})"
        encrypt(plaintext, destination, entry.private)
        return f"{destination} bijgewerkt (sleutel uit {entry.source})"

    def _apply_cluster(ctx2: Ctx) -> str:
        namespace = backup_ns_for(cluster.name)
        ctx2.kube.run(["apply", "-n", namespace, "-f", "-"], input_text=_deploy_secret_manifest())
        return f"secret minio-credentials in {namespace} bijgewerkt"

    def _restart(ctx2: Ctx) -> str:
        namespace = backup_ns_for(cluster.name)
        ctx2.kube.rollout_restart("deploy/minio", namespace=namespace)
        return f"minio in {namespace} herstart"

    def _verify(ctx2: Ctx) -> str:
        endpoint = f"http://minio.{backup_ns_for(cluster.name)}:9000"
        user = ctx2.current.get("BACKUP_S3_ACCESS_KEY", "backup-admin")
        return f"mc met nieuwe waarde: {_mc_check(ctx2, endpoint, user, ctx2.new[ctx2.rotating])}"

    return [
        Step(
            "Huidige waarde testen (mc tegen de backup-MinIO)",
            "check",
            [
                f"kubectl exec {OPI_DEPLOYMENT} -- mc alias set rot http://minio.{backup_ns_for(cluster.name)}:9000 <user> <HUIDIG> --api s3v4"
            ],
            lambda ctx3: (
                "mc met huidige waarde: "
                + _mc_check(
                    ctx3,
                    f"http://minio.{backup_ns_for(cluster.name)}:9000",
                    ctx3.current.get("BACKUP_S3_ACCESS_KEY", "backup-admin"),
                    ctx3.current[ctx3.rotating],
                )
            ),
        ),
        Step("Bestand bijwerken (SOPS-overlay bij backup-destination)", "file", [], _write),
        Step("Secret direct in het cluster applicen (via stdin, nooit via argv)", "sync", [], _apply_cluster),
        Step("MinIO herstarten", "restart", [], _restart),
        Step("Verifiëren met de nieuwe waarde (mc)", "verify", [], _verify),
    ]


def minio_verify_step(endpoint: str) -> Step:
    def _verify(ctx: Ctx) -> str:
        user = ctx.current.get("MINIO_ROOT_USER", "admin")
        fresh = _mc_check(ctx, endpoint, user, ctx.new[ctx.rotating])
        stale = _reject_stale(
            ctx,
            lambda: _mc_check(ctx, endpoint, user, ctx.current[ctx.rotating]),
            label="minio-root",
        )
        return f"mc met nieuwe waarde: {fresh}; {stale}"

    return Step("Verifiëren via mc: nieuwe waarde werkt, oude is afgewezen", "verify", [], _verify)


def env_verify_for(component: Component, cluster: Cluster) -> Step | None:
    """De app-check ná de herstart, per component dat er eentje heeft (minio via mc,
    mail-relay via zijn fallback-auth)."""
    if component.key == "minio":
        return minio_verify_step("http://minio:9000")
    if component.key == "mail-relay":
        return relay_auth_step(component, cluster, phase="verify", which="nieuwe")
    return None


def keycloak_mail_steps(kube: Kube, values: Ctx, relay_url: str, admin_current: dict[str, str]) -> list[Step]:
    """Het smtp-account zad-keycloak op de relay. De API-call loopt met het ADMIN-account,
    dus die handler hoort eerder in de ronde gedraaid (of ongemoeid) te zijn."""
    principal = "zad-keycloak"
    admin = admin_current.get("MAIL_RELAY_ADMIN_USERNAME", "admin")
    admin_password = admin_current.get("MAIL_RELAY_ADMIN_PASSWORD", "")
    payload_tpl = '[{"action":"set","field":"secrets","value":["%s"]}]'

    def _smtp_check(ctx: Ctx, password: str) -> str:
        host = relay_url.split("//", 1)[1].rsplit(":", 1)[0]
        snippet = (
            "import smtplib,os\n"
            "s=smtplib.SMTP(os.environ['H'],587,timeout=15)\n"
            "s.login('zad-keycloak', os.environ['W'])\n"
            "print('smtp-login OK')\n"
        )
        result = ctx.kube.exec(
            OPI_DEPLOYMENT, ["env", f"H={host}", f"W={password}", "python3", "-c", snippet], check=False
        )
        if result.returncode != 0 and not ctx.kube.dry_run:
            _ok(f"smtp-login faalde: {result.stderr.strip().splitlines()[-1][:150] if result.stderr.strip() else '?'}")
        return result.stdout.strip() if not ctx.kube.dry_run else "overgeslagen (dry-run)"

    def _apply(ctx: Ctx) -> str:
        # Stalwart v0.11: principal-update is PATCH /api/principal/<naam> met een lijst
        # PrincipalUpdates (action/field/value). POST bestaat niet (404) en 'property'
        # i.p.v. 'field' geeft 400 -- beide uitgeprobeerd en zo vastgelegd.
        outcome = relay_call(
            ctx.kube,
            relay_url,
            admin,
            admin_password,
            "PATCH",
            f"/api/principal/{principal}",
            payload_tpl % ctx.new[ctx.rotating],
        )
        if not relay_ok(outcome):
            _ok(f"relay weigerde de wijziging: {outcome[:150]}")
        return f"principal {principal} bijgewerkt ({outcome[:80]})"

    return [
        Step(
            "Huidige waarde testen (SMTP-login als zad-keycloak)",
            "check",
            ["smtplib.SMTP(relay, 587).login('zad-keycloak', <HUIDIG>) vanuit de OPI-pod"],
            lambda ctx: _smtp_check(ctx, ctx.current[ctx.rotating]),
        ),
        Step(
            "Nieuw wachtwoord op de relay zetten (als relay-admin)",
            "apply",
            [f"POST /api/principal/{principal} met nieuwe secrets (auth: relay-admin)"],
            _apply,
        ),
        Step(
            "Verifiëren: SMTP-login met de nieuwe waarde, oude afgewezen",
            "verify",
            [
                "SMTP-login met <NIEUW> (de herstart van deploy/keycloak is een aparte stap hierna)",
                "daarna SMTP-login met <HUIDIG>: die moet falen",
            ],
            lambda ctx: (
                f"smtp met nieuwe waarde: {_smtp_check(ctx, ctx.new[ctx.rotating])}; "
                + _reject_stale(ctx, lambda: _smtp_check(ctx, ctx.current[ctx.rotating]), label="smtp zad-keycloak")
            ),
        ),
    ]


def secret_name_of(component: Component) -> str:
    """De naam van het cluster-secret, uit de metadata van de template."""
    data = YAML().load((EDIT_TREES["infrastructure"][0] / component.template).read_text(encoding="utf-8"))
    return str(data["metadata"]["name"])


def cluster_secret_check(component: Component) -> Step:
    """Komt het cluster-secret overeen met het bestand? Dat is bij env-restart-componenten
    de vraag die telt: de app leest alleen bij start, dus bestand ↔ cluster IS de drift."""

    def _compare(ctx: Ctx) -> str:
        if ctx.kube.dry_run:
            return "overgeslagen (dry-run)"
        live = ctx.kube.secret_value(secret_name_of(component), ctx.rotating)
        if live is None:
            _ok(f"secret {secret_name_of(component)} staat niet in namespace {ctx.kube.namespace}")
        if live == ctx.current[ctx.rotating]:
            return "cluster-secret == bestand"
        _ok("cluster-secret wijkt af van het bestand")
        return ""

    return Step(
        "Cluster-secret vergelijken met het bestand",
        "check",
        [
            f"kubectl get secret {secret_name_of(component)} -o jsonpath - op veld {component.rotate_fields[0]}, geen wijziging"
        ],
        _compare,
    )


def github_pat_steps(kube: Kube, values: Ctx) -> list[Step]:
    """De gedeelde platform-PAT. GitHub kan zijn eigen tokens niet via zijn API aanmaken,
    dus de console blijft de enige handmatige stap; al het andere bewijst de tool via
    de GitHub-API (GET /user). Lokaal uitgevoerd, niet vanuit een pod: deze waarde hoort
    niet in een clusterproces te hoeven bestaan."""

    def _probe(token: str) -> str:
        snippet = (
            "import os,urllib.request\n"
            "req=urllib.request.Request('https://api.github.com/user')\n"
            "req.add_header('Authorization','Bearer '+os.environ['T'])\n"
            "print(urllib.request.urlopen(req, timeout=15).status)\n"
        )
        process = subprocess.run(  # noqa: S603
            [sys.executable, "-c", snippet],
            env={**os.environ, "T": token},
            capture_output=True,
            text=True,
            check=False,
        )
        if process.returncode != 0 or "200" not in process.stdout:
            _ok(
                f"GitHub-API met deze token: rc={process.returncode} {process.stdout.strip()} {process.stderr.strip()[:120]}"
            )
        return "HTTP 200"

    return [
        Step(
            "Huidige token testen tegen de GitHub-API",
            "check",
            ["GET https://api.github.com/user met de huidige token, lokaal"],
            lambda ctx: "huidige token: " + _probe(ctx.current["PROJECT_REPO_PASSWORD"]),
        ),
        Step(
            "EXTERN: maak een nieuwe token aan in de GitHub-console",
            "manual",
            [
                "Settings → Developer settings → Fine-grained tokens, scope zoals de huidige.",
                "De oude blijft werken tot je hem zelf revoket; doe dat pas als ZAD op de nieuwe draait.",
            ],
            None,
        ),
        Step(
            "Verifiëren via de GitHub-API",
            "verify",
            ["GET https://api.github.com/user met de nieuwe token, lokaal"],
            lambda ctx: "nieuwe token: " + _probe(ctx.new["PROJECT_REPO_PASSWORD"]),
        ),
    ]


def transip_steps(kube: Kube, values: Ctx) -> list[Step]:
    def _verify_key(account: str, private_key: str) -> str:
        import asyncio

        from opi.connectors.transip import TransIPConnector

        connector = TransIPConnector(account, private_key)
        domains = asyncio.run(connector.list_domains())
        return f"{len(domains)} domeinen gelezen"

    def _check(ctx: Ctx) -> str:
        return "huidige sleutel via API: " + _verify_key(
            ctx.current["TRANSIP_ACCOUNT_NAME"], ctx.current["TRANSIP_PRIVATE_KEY"]
        )

    def _verify(ctx: Ctx) -> str:
        return "nieuwe sleutel via API: " + _verify_key(ctx.new["TRANSIP_ACCOUNT_NAME"], ctx.new["TRANSIP_PRIVATE_KEY"])

    return [
        Step(
            "Huidige sleutel testen tegen de TransIP-API",
            "check",
            ["TransIPConnector(account, key).list_domains(), lokaal"],
            _check,
        ),
        Step(
            "EXTERN: maak een nieuw sleutelpaar aan in de TransIP-console",
            "manual",
            [
                "TransIP heeft geen API om zijn eigen API-sleutel te roteren. Maak het nieuwe "
                "paar in de console (Account → API), desactiveer daar later het oude."
            ],
            None,
        ),
        Step(
            "Verifiëren via de TransIP-API",
            "verify",
            ["TransIPConnector(account, nieuwe-key).list_domains(), lokaal"],
            _verify,
        ),
    ]


def grafana_steps(kube: Kube, values: Ctx) -> list[Step]:
    def _probe(ctx: Ctx, token: str) -> str:
        snippet = (
            "import os,urllib.request\n"
            "req=urllib.request.Request(os.environ['GRAFANA_URL'].rstrip('/')+'/api/org')\n"
            "req.add_header('Authorization','Bearer '+os.environ['T'])\n"
            "print(urllib.request.urlopen(req, timeout=15).status)\n"
        )
        result = ctx.kube.exec(OPI_DEPLOYMENT, ["env", f"T={token}", "python3", "-c", snippet], check=False)
        if result.returncode != 0 and not ctx.kube.dry_run:
            _ok(f"grafana /api/org gaf geen 200: {result.stderr.strip()[:150]}")
        return result.stdout.strip() if not ctx.kube.dry_run else "overgeslagen (dry-run)"

    return [
        Step(
            "Huidige token testen tegen de Grafana-API",
            "check",
            ["GET $GRAFANA_URL/api/org met Bearer <HUIDIG>, vanuit de OPI-pod"],
            lambda ctx: "huidige token: " + _probe(ctx, ctx.current["GRAFANA_TOKEN"]),
        ),
        Step(
            "EXTERN: vraag een nieuwe service-account-token bij ODCN",
            "manual",
            [
                "De Grafana van ODCN staat buiten dit cluster. Vraag daar een nieuwe token; "
                "vervang de oude pas als deze rotatie rond is."
            ],
            None,
        ),
        Step(
            "Verifiëren via de Grafana-API vanuit de OPI-pod",
            "verify",
            ["GET $GRAFANA_URL/api/org met Bearer <NIEUW>"],
            lambda ctx: "nieuwe token: " + _probe(ctx, ctx.new["GRAFANA_TOKEN"]),
        ),
    ]


# ---------------------------------------------------------------------------
# Planbouw en uitvoering
# ---------------------------------------------------------------------------


def template_path_of(component: Component) -> Path | None:
    """De template van dit component, of None: niet elk secret in de overlays is ooit
    door de generatie gekomen (transip-secret heeft er geen), en toch moet het roteerbaar
    zijn. Zonder template werken we op de ontsleutelde inhoud zelf."""
    path = EDIT_TREES["infrastructure"][0] / component.template if component.template else None
    return path if (path is not None and path.is_file()) else None


@dataclass(frozen=True)
class Destination:
    """Waar het bestand van een component werkelijk ligt, en hoe.

    state:
    - ``canonical``         -- op de plek de template-afleiding voorschrijft, SOPS-versleuteld;
    - ``legacy-encrypted``  -- versleuteld maar op een oudere/afwijkende naam;
    - ``plaintext``         -- het template dat nooit door de generatie is gegaan en plat in
                               de overlay wordt toegepast (het geval keycloak-db op odcn);
    - ``absent``            -- er is geen bestand in deze overlay.
    """

    canonical: Path
    existing: Path | None
    state: str


def resolve_destination(overlays: Path, template: str) -> Destination:
    """Zoek het bestaande bestand, met de namen die in het wild voorkomen.

    De canonieke vorm is `<template>.yaml.sops.yaml`; in het wild bestaan ook de kaal
    gebezigde templatenaam en de variant waarin '-secret' uit de naam is geknipt (zo is
    `keycloak-db-credentials.yaml` ontstaan). Een bestand zonder `sops:`-blok is per
    definitie plaintext, wat zijn naam ook is.
    """
    canonical = overlays / encrypted_name(template)
    stripped = template[: -len("-secret.yaml")] + ".yaml" if template.endswith("-secret.yaml") else template
    candidates = [
        canonical,
        overlays / template,
        overlays / (stripped + ".sops.yaml"),
        overlays / stripped,
    ]
    for path in candidates:
        if not path.is_file():
            continue
        has_sops = re_search_sops(path)
        if path == canonical:
            return Destination(canonical, path, "canonical")
        return Destination(canonical, path, "legacy-encrypted" if has_sops else "plaintext")
    return Destination(canonical, None, "absent")


def re_search_sops(path: Path) -> bool:
    """Heeft dit bestand een sops-metadata-blok (dus: is het versleuteld)?"""
    return bool(re.search(r"^sops:", path.read_text(encoding="utf-8"), re.MULTILINE))


def destination_for(component: Component, cluster: Cluster) -> Path:
    """De bestaande plek voor dit cluster: de gewireerde realiteit als die er is, anders
    de canonieke naam (voor 'bestaat niet'-meldingen)."""
    resolution = resolve_destination(
        EDIT_TREES["infrastructure"][1] / cluster.folders["infrastructure"], component.template
    )
    return resolution.existing or resolution.canonical


def read_current(component: Component, cluster: Cluster) -> dict[str, str]:
    """De huidige waarden van dit component, waar het bestand ook ligt.

    Canoniek versleuteld en legacy-versleuteld gaan via de recipient+keyring; een
    plaintext-bestand (het geval keycloak-db op odcn: het template dat nooit door de
    generatie ging) wordt als zichzelf gelezen -- zijn waarden ZIJN de huidige waarden.
    Rotatie zonder bestand kan niet, want dan is er geen 'huidig' om de app mee te openen.
    """
    overlays = EDIT_TREES["infrastructure"][1] / cluster.folders["infrastructure"]
    resolution = resolve_destination(overlays, component.template)
    if resolution.state == "plaintext":
        return values_of(resolution.existing.read_text(encoding="utf-8"))  # type: ignore[union-attr]
    if resolution.state == "absent" or resolution.existing is None:
        raise EditFailed(f"{resolution.canonical} bestaat niet - maak het eerst aan (edit-modus)")
    destination = resolution.existing
    found = sops_recipients(destination)
    if not found:
        raise EditFailed(f"{destination} heeft geen AGE-recipient in zijn SOPS-metadata")
    return values_of(decrypt(destination, key_entry_for(found[0]).private))


def update_wiring(overlays: Path, *, old_plain: str, new_encrypted: str) -> list[str]:
    """Verbind het nieuwe SOPS-bestand en knip het plaintext bestand uit de render.

    Twee YAML-edits in de overlay: het bestand komt bij de `files:`-lijst van
    decrypt-sops.yaml (zodat ksops het bij de build ontsleutelt), en de oude regel uit de
    `resources:`-lijst van kustomization.yaml (die verwees naar het plaintext bestand zelf).
    """
    changed: list[str] = []
    yaml = YAML()
    yaml.preserve_quotes = True

    decrypt_file = overlays / "decrypt-sops.yaml"
    data = yaml.load(decrypt_file.read_text(encoding="utf-8"))
    files = data.get("files") or []
    if new_encrypted not in files:
        files.append(new_encrypted)
        data["files"] = files
        _write_yaml(decrypt_file, data)
    changed.append(f"{decrypt_file.name}: {new_encrypted} toegevoegd aan files")

    kustomization_file = overlays / "kustomization.yaml"
    kust = yaml.load(kustomization_file.read_text(encoding="utf-8"))
    resources = kust.get("resources") or []
    if old_plain in resources:
        resources.remove(old_plain)
        kust["resources"] = resources
        _write_yaml(kustomization_file, kust)
        changed.append(f"{kustomization_file.name}: {old_plain} uit resources gehaald")
    return changed


def _write_yaml(path: Path, data: object) -> None:
    from io import StringIO

    yaml = YAML()
    yaml.preserve_quotes = True
    stream = StringIO()
    yaml.dump(data, stream)
    path.write_text(stream.getvalue(), encoding="utf-8")


def sibling_recipients(overlays: Path) -> list[str]:
    """De recipient-set van een buur-SOPS-bestand in dezelfde overlay.

    Bij de eerste versleuteling van een bestand (de plaintext-migratie) is er nog geen
    eigen metadata om te volgen; het secret hoort dan bij dezelfde sleutel(s) als zijn
    buren, niet bij toevallig de sleutel die in je keyring bovenaan staat.
    """
    for path in sorted(overlays.glob("*.sops.yaml")):
        found = sops_recipients(path)
        if found:
            return found
    raise EditFailed(f"geen versleuteld buurbestand in {overlays} om de recipient-set van over te nemen")


def file_step(component: Component, cluster: Cluster) -> Step:
    """De bestandskant van de rotatie. Drie toestanden, drie vormen:

    - canoniek/legacy-versleuteld: alle velden behouden, alleen de geroteerde vervangen;
    - plaintext (template dat nooit gegenereerd is): dat is de migratie -- schrijf het
      canonieke versleutelde bestand, verbind het in decrypt-sops.yaml en kustomization.yaml,
      en verwijder het plaintext bestand. Alles in één ronde, anders staat git even met
      twee bronnen van hetzelfde secret.
    """
    overlays = EDIT_TREES["infrastructure"][1] / cluster.folders["infrastructure"]
    resolution = resolve_destination(overlays, component.template)

    def _write(ctx: Ctx) -> str:
        template_path = template_path_of(component)
        # Waarborg: een geroteerd veld mag nooit leeg geschreven worden; een leeg secret
        # in git is erger dan geen rotatie, en het heeft zich in het wild ook zo voorgedaan.
        for name in component.rotate_fields:
            if not ctx.new.get(name):
                raise EditFailed(f"nieuwe waarde voor {name} is leeg; schrijven geweigerd")
        if template_path is None:
            # Geen template (transip): werk op de ontsleutelde inhoud en vervang alleen
            # de geroteerde velden. Alles anders dan die velden blijft byte-voor-byte.
            destination = resolution.existing
            if (resolution.state != "canonical" and resolution.state != "legacy-encrypted") or destination is None:
                raise EditFailed(f"{component.key}: onverwachte bestandstoestand ({resolution.state})")
            entry = key_entry_for(sops_recipients(destination)[0])
            yaml = YAML()
            yaml.preserve_quotes = True
            data = yaml.load(decrypt(destination, entry.private))
            for name in component.rotate_fields:
                data["stringData"][name] = ctx.new[name]
            from io import StringIO

            stream = StringIO()
            yaml.dump(data, stream)
            plaintext = stream.getvalue()
            encrypt(plaintext, destination, entry.private)
            _register_paths(ctx, destination)
            return f"{destination} bijgewerkt (sleutel uit {entry.source})"

        values = dict(ctx.current)
        for name in component.rotate_fields:
            values[name] = ctx.new[name]
        plaintext = apply(template_path.read_text(encoding="utf-8"), values, ctx.cluster.namespace)

        if resolution.state == "plaintext":
            recipients = sibling_recipients(overlays)
            entry = key_entry_for(recipients[0])
            encrypt(plaintext, resolution.canonical, entry.private, recipients=recipients)
            changed = update_wiring(
                overlays,
                old_plain=resolution.existing.name,  # type: ignore[union-attr]
                new_encrypted=resolution.canonical.name,
            )
            _register_paths(
                ctx,
                resolution.canonical,
                overlays / "decrypt-sops.yaml",
                overlays / "kustomization.yaml",
                resolution.existing,  # type: ignore[arg-type]
            )
            resolution.existing.unlink()  # type: ignore[union-attr]
            changed.append(f"{resolution.existing.name} verwijderd (plaintext)")  # type: ignore[union-attr]
            return "MIGRATIE+rotatie: " + "; ".join([f"{resolution.canonical.name} versleuteld aangemaakt", *changed])

        if resolution.existing is None:
            raise EditFailed(f"{resolution.canonical} bestaat niet - maak het eerst aan (edit-modus)")
        entry = key_entry_for(sops_recipients(resolution.existing)[0])
        encrypt(plaintext, resolution.existing, entry.private)
        _register_paths(ctx, resolution.existing)
        note = f"sleutel uit {entry.source}"
        if resolution.state == "legacy-encrypted":
            note += f"; let op: legacy-naam {resolution.existing.name} blijft staan (verhuizing apart opruimen)"
        return f"{resolution.existing} bijgewerkt ({note})"

    title = f"Bestand bijwerken: {resolution.existing.name if resolution.existing else resolution.canonical.name}"
    if resolution.state == "plaintext":
        title += " (migratie: plaintext → SOPS + bedrading)"
    return Step(
        title,
        "file",
        [f"schrijf {resolution.canonical} met SOPS (alleen {', '.join(component.rotate_fields)} gewijzigd)"],
        _write,
    )


def env_step(component: Component, cluster: Cluster) -> Step:
    """De kopie in het ZAD-envbestand meenemen, als dit veld daar een dubbele heeft."""
    path = env_file_for(cluster.name)
    pairs = list(zip(component.rotate_fields, component.zad_env, strict=True))

    def _write(ctx: Ctx) -> str:
        if not path.exists():
            return (
                f"{path} bestaat op deze machine niet - werk daar zelf "
                + ", ".join(name for _, name in pairs)
                + " bij en regenereer het env-secret"
            )
        updated = write_env_file(path, {env: ctx.new[field] for field, env in pairs})
        return (
            f"{path} bijgewerkt: {', '.join(updated)}. Regenereer het env-secret met "
            "'task generate-env-secrets-for-operations-manager' en herstart ZAD"
        )

    return Step(
        "ZAD-envbestand meenemen" + (f" ({path.name})" if not path.exists() else ""),
        "env",
        [f"{name}=<NIEUW> in {path.name}" for _, name in pairs],
        _write,
    )


def steps_for(
    component: Component, cluster: Cluster, kube: Kube, ctx: Ctx, *, defer_restarts: bool = False
) -> list[Step]:
    """Het hele plan voor één component. De categorie is leidend voor de volgorde:

    - cnpg-secret:  drift+login-check → bestand → git-poort → sync+bewijs → operator-gate → herstart
    - live-app:     check → app-call → verify nieuw → bestand → git-poort → sync+bewijs → herstart
    - env-restart:  drift-check → bestand → git-poort → sync+bewijs → herstart → app-verify
    - external:     check → handmatige externe stap → verify → bestand/env (geen cluster-sync-poort)

    Bij meerdere componenten in één ronde (--all of een rij) kan de aanroeper restarts en
    post-restart-verificaties uitstellen naar één slotafronding (`defer_restarts`): het
    cluster blijft dan zolang consistent met de draaiende pods, en ZAD/keycloak herstarten
    één keer aan het einde in plaats van per geroteerd wachtwoord.
    """
    minio_endpoint = "http://minio:9000"

    if component.category == "external":
        builders = {"transip": transip_steps, "grafana": grafana_steps, "platform-repo-pat": github_pat_steps}
        builder = builders.get(component.key)
        if builder is None:
            raise EditFailed(f"external-component zonder bouwer: {component.key}")
        steps = builder(kube, ctx)
        if component.template:
            steps = [*steps, file_step(component, cluster)]
        if component.zad_env:
            steps = [*steps, env_step(component, cluster)]
        followup = followup_step(component)
        return [*steps] + ([followup] if followup else [])

    steps: list[Step] = []
    if component.category in ("cnpg-secret", "env-restart"):
        steps.append(cluster_secret_check(component))

    if component.category == "cnpg-secret":
        steps.append(cnpg_check_step(component))
        steps.append(file_step(component, cluster))
    elif component.category == "live-app":
        if component.key == "redis":
            steps.extend(redis_steps(kube, ctx))
        elif component.key == "postgresql":
            steps.extend(pg_superuser_steps(kube, ctx))
        elif component.key == "keycloak-admin":
            steps.extend(keycloak_admin_steps(kube, ctx))
        elif component.key == "keycloak-mail":
            admin = read_current(BY_KEY["mail-relay"], cluster)
            steps.extend(keycloak_mail_steps(kube, ctx, relay_api_for(cluster.name), admin))
        else:
            raise EditFailed(f"live-app zonder bouwer: {component.key}")
        steps.append(file_step(component, cluster))
    elif component.category == "bootstrap":
        steps = bootstrap_component_steps(component, cluster, kube, ctx)
        if component.zad_env:
            steps.append(env_step(component, cluster))
        followup = followup_step(component)
        if followup is not None:
            steps.append(followup)
        return steps
    elif component.category == "env-restart":
        if component.key == "minio":
            steps.append(minio_check_step(kube, ctx, minio_endpoint))
        elif component.key == "mail-relay":
            steps.append(relay_auth_step(component, cluster, phase="check", which="huidige"))
        steps.append(file_step(component, cluster))
    else:
        raise EditFailed(f"onbekende categorie: {component.category}")

    steps.append(push_step(component, cluster))
    steps.append(sync_step(component))

    if component.category == "cnpg-secret":
        steps.append(cnpg_operator_verify_step(component))
    restart = None if defer_restarts else restart_step(component, cluster)
    if restart is not None:
        steps.append(restart)
    if component.category == "env-restart" and not defer_restarts:
        verify = env_verify_for(component, cluster)
        if verify is not None:
            steps.append(verify)

    if component.zad_env:
        steps.append(env_step(component, cluster))
    followup = followup_step(component)
    if followup is not None:
        steps.append(followup)
    return steps


def target_of(workload: str, cluster: Cluster) -> tuple[str, str]:
    """Een workload-string naar (namespace, naam). Prefiks `relayns:` betekent: de
    relay-namespace van dit cluster, afgeleid van de management-URL in RELAY_API."""
    if workload.startswith("relayns:"):
        host = relay_api_for(cluster.name).split("//", 1)[1]
        relay_namespace = host.split(".")[1]
        return relay_namespace, workload.split(":", 1)[1]
    return "", workload


def _git(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Git in de repo-root, met de foutstijl van de rest van de tool.

    Altijd `-C REPO`: de rotatie wordt ook uit een subdirectory gestart, en dan wijst een
    relatief pad iets anders aan dan het bestand dat de bestandsstap net schreef.
    """
    process = subprocess.run(  # noqa: S603
        ["git", "-C", str(REPO), *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if check and process.returncode != 0:
        detail = (process.stderr or process.stdout).strip()
        raise EditFailed(f"git {' '.join(args)}: {detail[:300]}")
    return process


def _register_paths(ctx: Ctx, *paths: Path) -> None:
    """Leg vast welke bestanden deze ronde zijn aangeraakt, zodat de push-stap exact die
    staget. Niet de hele overlay-map: daar kan onverwant werk van iemand anders liggen dat
    niet in een secret-rotatie thuishoort.
    """
    ctx.extra["git_paths"] = "\n".join(str(path.relative_to(REPO)) for path in paths)


def _touched_paths(ctx: Ctx) -> list[str]:
    """De paden die de bestandsstap registreerde, als repo-relatieve strings."""
    return [line for line in ctx.extra.get("git_paths", "").splitlines() if line]


def _same_repo(left: str, right: str) -> bool:
    """Of twee git-URL's dezelfde repo aanwijzen, ongeacht hun vorm.

    `git@github.com:org/repo.git` en `https://user@github.com/org/repo` zijn dezelfde plek.
    Een letterlijke vergelijking zegt van niet, en dan zou de push op de verkeerde remote
    landen of helemaal niet gevonden worden.
    """

    def normalise(url: str) -> str:
        url = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", url.strip())
        url = re.sub(r"^[^/@]*@", "", url)
        url = url.replace(":", "/", 1)
        return re.sub(r"/{2,}", "/", url).removesuffix(".git").rstrip("/").lower()

    return normalise(left) == normalise(right)


def git_target_of(kube: Kube, app: str, app_namespace: str) -> tuple[str, str]:
    """De remote en branch waaruit deze Applicatie leest, als (remote, branch).

    De waarheid staat in de Applicatie (`spec.source.repoURL` en `targetRevision`), niet in
    de git-config. Deze checkout heeft meerdere remotes die ver uiteen kunnen lopen (GitHub
    en Forgejo stonden op 07-10-2026 2693 commits uit elkaar), dus "push naar de upstream
    van je branch" is precies de verkeerde plek. Leest de Applicatie uit een repo die hier
    geen remote is, zoals de in-cluster Forgejo van de sandbox, dan faalt dit met die uitleg
    in plaats van ergens anders te landen.
    """
    source = kube.run(
        [
            "get",
            APPS_RESOURCE,
            app,
            "-n",
            app_namespace,
            "-o",
            "jsonpath={.spec.source.repoURL}{'|'}{.spec.source.targetRevision}",
        ]
    )
    repo_url, _, revision = (source.stdout or "").strip().partition("|")
    if not repo_url:
        raise EditFailed(f"Applicatie {app} heeft geen spec.source.repoURL om naartoe te pushen")
    branch = revision.strip() or "HEAD"
    for line in _git(["remote", "-v"]).stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and _same_repo(parts[1], repo_url):
            return parts[0], branch
    raise EditFailed(
        f"Applicatie {app} leest uit {repo_url} ({branch}), en dat is geen remote van deze checkout. "
        "Push de commit zelf naar die repo (op de sandbox gaat dat met `task sandbox:sync`) en draai daarna verder."
    )


def push_step(component: Component, cluster: Cluster) -> Step:
    """Stage, commit en push het gewijzigde secret, en bewijs dat het op de remote staat.

    Dit was handwerk, zodat een mens de security-diff zou reviewen. Dat kostte op
    07-10-2026 de hele Applicatie: de voorgestelde `git add` noemde alleen het oude platte
    bestand, de drie andere paden van de migratie bleven ongecommit, en de sync erna bouwde
    een kustomization die naar een verwijderd bestand wees. Reviewen kan nog steeds, de stap
    is met 'n' over te slaan en de diff staat in je werkboom, maar het staging-werk hoort
    bij de tool: die weet exact welke paden hij heeft aangeraakt en een mens niet.
    """
    fields = ", ".join(component.rotate_fields)
    message = f"chore(secrets): roteer {fields} van {component.key} op {cluster.name}"

    def _run(ctx: Ctx) -> str:
        if ctx.kube.dry_run:
            return "overgeslagen (dry-run)"
        paths = _touched_paths(ctx)
        if not paths:
            raise EditFailed("de bestandsstap registreerde geen paden; is die stap overgeslagen?")
        _git(["add", "--", *paths])
        if _git(["diff", "--cached", "--quiet", "--", *paths], check=False).returncode == 0:
            raise EditFailed(f"niets om te committen in {', '.join(paths)}")
        _git(["commit", "--no-verify", "-m", message])
        head = _git(["rev-parse", "HEAD"]).stdout.strip()

        name = secret_name_of(component)
        owner = argo_owner_of(ctx.kube, name)
        if owner is None:
            raise EditFailed(
                f"secret {name} heeft geen Argo-eigenaar in {ctx.kube.namespace}, dus de doel-repo is "
                f"onbekend. De commit ({head[:9]}) staat lokaal; push hem zelf en draai daarna verder."
            )
        remote, branch = git_target_of(ctx.kube, *owner)
        _git(["push", remote, f"HEAD:{branch}"])
        landed = _git(["ls-remote", remote, branch]).stdout.split()
        if not landed or landed[0] != head:
            tip = landed[0][:9] if landed else "leeg"
            raise EditFailed(f"{head[:9]} staat na de push niet op {remote}/{branch} (remote staat op {tip})")
        return f"{head[:9]} op {remote}/{branch}: {', '.join(paths)}"

    return Step(
        "Git: stage, commit en push het gewijzigde secret",
        "push",
        [
            "git add -- <de paden die de bestandsstap aanraakte>",
            f"git commit --no-verify -m '{message}'",
            "git push <remote van de Applicatie> HEAD:<targetRevision>",
            "daarna bewijst git ls-remote dat de commit op die branch staat",
        ],
        _run,
    )


def _assert_in_git(ctx: Ctx, app: str, app_namespace: str) -> None:
    """Weiger te syncen zolang de wijziging niet volledig in git en op de remote staat.

    Argo leest de remote, dus een ongecommitte wijziging synct de oude stand. Erger: is het
    platte bestand wel verwijderd maar de kustomization niet gepusht, dan faalt de
    kustomize-build en blokkeert de hele Applicatie, ook voor onverwant werk in dezelfde
    boom. Dat gebeurde op 07-10-2026. De werkboom-toets geldt altijd; de remote-toets slaat
    over als de doel-repo hier geen remote is (de sandbox pusht via `task sandbox:sync`),
    want dan is er niets te meten.
    """
    paths = _touched_paths(ctx)
    if not paths:
        return
    dirty = _git(["status", "--porcelain", "--", *paths]).stdout.strip()
    if dirty:
        raise EditFailed(
            "deze paden staan nog niet (volledig) in git: "
            + "; ".join(line.strip() for line in dirty.splitlines())
            + ". Draai de push-stap, of commit en push ze zelf, voor je synct."
        )
    try:
        remote, branch = git_target_of(ctx.kube, app, app_namespace)
    except EditFailed:
        return
    head = _git(["rev-parse", "HEAD"]).stdout.strip()
    _git(["fetch", remote, branch], check=False)
    if _git(["merge-base", "--is-ancestor", head, "FETCH_HEAD"], check=False).returncode != 0:
        raise EditFailed(f"{head[:9]} zit niet in {remote}/{branch}, en Argo leest die branch: push hem eerst")


def sync_step(component: Component) -> Step:
    """Refresh én sync de eigenaar-Applicatie via de Kubernetes-API, bewijs de afloop én
    bewijs dat het cluster-secret de nieuwe waarde draagt. Pas dan mogen restarts volgen.

    Geen Argo-eigenaar gevonden betekent: dit secret staat niet onder GitOps (bootstrap),
    en dan stopt het hier met de melding welk pad dat wel vraagt -- in plaats van stil
    te pollen op een sync die nooit komt.
    """

    def _run(ctx: Ctx) -> str:
        if ctx.kube.dry_run:
            return "overgeslagen (dry-run)"
        name = secret_name_of(component)
        owner = argo_owner_of(ctx.kube, name)
        if owner is None:
            raise EditFailed(
                f"secret {name} heeft geen Argo-eigenaar in {ctx.kube.namespace}: het staat niet onder "
                "GitOps. Dit secret hoort bij het bootstrap-pad en moet rechtstreeks worden ge-applied; "
                "die componentrij ontbreekt nog (zie plans/wachtwoorden-roteren-flow-analyse.md § 4)."
            )
        app, app_namespace = owner
        _assert_in_git(ctx, app, app_namespace)
        synced = argo_refresh_and_sync(ctx.kube, app, app_namespace)
        arrived = wait_for_secret_value(ctx.kube, name, ctx.rotating, ctx.new[ctx.rotating])
        return f"{synced} ; {arrived}"

    return Step(
        "Argo-sync starten en afwachten; cluster-secret moet de nieuwe waarde dragen",
        "sync",
        [
            "kubectl annotate application <eigenaar> argocd.argoproj.io/refresh=hard --overwrite",
            'kubectl patch application <eigenaar> --type=merge -p \'{"operation":{"sync":{}}}\'',
            "poll: .status.operationState.phase == Succeeded; daarna: secret == <NIEUW>",
        ],
        _run,
    )


def restart_step(component: Component, cluster: Cluster) -> Step | None:
    """Consumers herstarten die de waarde bij start inlezen. Staat NA de sync-gate en
    (bij cnpg) na de operator-gate. Een workload die op dit cluster niet bestaat wordt
    gemeld en overgeslagen, niet stil geslikt en ook niet als fatale fout."""
    if not component.workloads:
        return None

    def _run(ctx: Ctx) -> str:
        done: list[str] = []
        for workload in component.workloads:
            namespace, name = target_of(workload, ctx.cluster)
            namespace = namespace or ctx.kube.namespace
            exists = ctx.kube.run(["get", name, "-n", namespace], check=False)
            if exists.returncode != 0:
                if ctx.kube.dry_run:
                    done.append(f"{name} (dry-run)")
                    continue
                done.append(f"{name} overgeslagen (bestaat niet in {namespace})")
                continue
            ctx.kube.rollout_restart(name, namespace=namespace)
            done.append(f"{name} herstart ({namespace})")
        return " ; ".join(done)

    names = ", ".join(component.workloads)
    return Step(
        f"Consumers herstarten ({names})",
        "restart",
        [
            f"kubectl rollout restart {target_of(w, cluster)[1]} (-n {target_of(w, cluster)[0] or '<namespace van OPI>'})"
            for w in component.workloads
        ],
        _run,
    )


def final_restart_step(components: list[Component], cluster: Cluster) -> Step | None:
    """De slotafronding van een bulkronde: alle benodigde herstarts, gededupliceerd, één keer.

    OPI staat bewust onderaan: die herstart is het duurst, en alle component-verificaties
    in de ronde spreken rechtstreeks met hun eigen app, dus niets in de ronde hangt van de
    ZAD-binnenkant. Een uitgestelde herstart is veilig omdat een pod zijn omgeving alleen
    bij start inleest.
    """
    ordered: list[str] = []
    for component in components:
        for workload in component.workloads:
            if workload not in ordered:
                ordered.append(workload)
    if not ordered:
        return None
    ordered.sort(key=lambda w: w == OPI_DEPLOYMENT)  # OPI als allerlaatste

    def _run(ctx: Ctx) -> str:
        done: list[str] = []
        for workload in ordered:
            namespace, name = target_of(workload, ctx.cluster)
            namespace = namespace or ctx.kube.namespace
            exists = ctx.kube.run(["get", name, "-n", namespace], check=False)
            if exists.returncode != 0 and not ctx.kube.dry_run:
                done.append(f"{name} overgeslagen (bestaat niet in {namespace})")
                continue
            ctx.kube.rollout_restart(name, namespace=namespace)
            done.append(f"{name} herstart ({namespace})")
        return " ; ".join(done)

    return Step(
        f"Alle consumers herstarten, gededupliceerd ({', '.join(ordered)})",
        "restart",
        [f"kubectl rollout restart {target_of(w, cluster)[1]}" for w in ordered],
        _run,
    )


def deferred_verify_steps(components: list[Component], cluster: Cluster) -> list[Step]:
    """De post-restart-verificaties die in een bulkronde wachtten op de slotafronding.
    Elke env-restart-component met zo'n check staat hier (vandaag: minio, mail-relay)."""
    return [step for component in components for step in [env_verify_for(component, cluster)] if step is not None]


def followup_step(component: Component) -> Step | None:
    """Alleen de component-aantekening; sync en herstarten zijn inmiddels stappen ín de
    ronde, geen verzoekjes erachteraan."""
    if not component.note:
        return None
    return Step("Aantekening bij dit component", "followup", [component.note], None)


# ---------------------------------------------------------------------------
# Seeding: een secrets-overlay kanoniek maken vanaf het live cluster
# ---------------------------------------------------------------------------
#
# Verhaal erbij: de sandbox kreeg zijn infra-secrets uit de Forgejo-mirror
# (zad-argo-infrastructure), met plaintext dev-waarden, en de bijbehorende overlay heeft
# in deze repo nooit bestaan. Daarmee is `overlays/sandboxed-local` hier de afwezige bron
# van een draaiende werkelijkheid -- drift van de zogenaamde geredde kant. Deze functie
# schrijft die overlay: voor elke template de LIVE waarden uit het cluster, versleuteld
# met de sleutel van dit cluster, plus de bedrading (decrypt-sops.yaml + kustomization.yaml)
# in precies de vorm die `odcn` kent. Daarna kan `task sandbox:sync` hem naar de mirror
# brengen en is er weer één bron.


SEED_KEYS = {
    "sandboxed-local": "security/sandbox-key.txt",
    "odcn-production": "security/key.txt",
}


def seed_overlay(cluster: Cluster, kube: Kube, *, apply_changes: bool) -> int:
    """Maak de secrets-overlay van dit cluster in deze repo, gevoed met live waarden.

    Leest per template het live cluster-secret (naam uit de template-metadata, data uit
    `kubectl get secret -o json`), rendert de template met die waarden en versleutelt.
    Alles staat daarna gelijk aan live: een eerste `--check --all` mag dan geen drift
    melden. Secrets in het cluster zonder template worden bij naam gemeld (niet geseed).
    """
    templates_dir, overlays_root, _ = EDIT_TREES["infrastructure"]
    overlays = overlays_root / cluster.folders["infrastructure"]
    key_file = REPO / SEED_KEYS[cluster.name]
    if not key_file.is_file():
        raise EditFailed(f"conventie-sleutel voor {cluster.name} ontbreekt: {key_file}")
    private = read_key(key_file)
    public = public_key_of(private)

    if overlays.exists():
        raise EditFailed(f"{overlays} bestaat al - seeden gebeurt maar één keer; gebruik de rotatieflow")

    planned: list[str] = []
    written: list[str] = []
    skipped: list[str] = []
    encrypted_files: list[str] = []
    for template in templates(templates_dir):
        template_data = YAML().load(template.read_text(encoding="utf-8"))
        secret_name = str(template_data["metadata"]["name"])
        found = kube.run(["get", "secret", secret_name, "-n", kube.namespace, "-o", "json"], check=False)
        if found.returncode != 0 or not found.stdout:
            skipped.append(f"{template.name} (geen secret {secret_name} in {kube.namespace})")
            continue
        data = json.loads(found.stdout).get("data") or {}
        values = {str(name): base64.b64decode(value).decode() for name, value in data.items()}
        planned.append(f"{secret_name}: {len(values)} velden")
        if not apply_changes:
            continue
        encrypted_name_here = encrypted_name(template.name)
        plaintext = apply(template.read_text(encoding="utf-8"), values, cluster.namespace)
        overlays.mkdir(parents=True, exist_ok=True)
        encrypt(plaintext, overlays / encrypted_name_here, private)
        written.append(f"{encrypted_name_here} ({secret_name})")
        encrypted_files.append(encrypted_name_here)

    print(f"\nOverlay: {overlays}")
    print(f"Cluster: {cluster.name} (namespace {kube.namespace})")
    print(f"Sleutel: {key_file} ({public[:20]}…)")
    print("\nGelezen uit het cluster:")
    for line in planned:
        print(f"  {line}")
    for line in skipped:
        print(f"  overgeslagen: {line}")

    if not apply_changes:
        print(f"\nDRY-RUN: niets geschreven. Met --apply: {len(planned)} versleutelde bestanden + bedrading.")
        return 0

    decrypt = overlays / "decrypt-sops.yaml"
    decrypt.write_text(
        "apiVersion: viaduct.ai/v1\nkind: ksops\nmetadata:\n  name: secret-generator\n  annotations:\n"
        '    config.kubernetes.io/function: "exec:\\n  path: ksops\\n"\nfiles:\n'
        + "".join(f"  - {name}\n" for name in encrypted_files),
        encoding="utf-8",
    )
    kustomization = overlays / "kustomization.yaml"
    kustomization.write_text(
        "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\n\ngenerators:\n- decrypt-sops.yaml\n",
        encoding="utf-8",
    )
    print(f"\nGeschreven: {len(written)} secrets + decrypt-sops.yaml + kustomization.yaml")
    for line in written:
        print(f"  {line}")
    print("\nVolgende stap buiten dit script: review, commit, push — op sandbox daarna `task sandbox:sync`.")
    return 0


# ---------------------------------------------------------------------------
# Orkestratie: nieuwe waarden ophalen, checken, roteren
# ---------------------------------------------------------------------------

MULTILINE_FIELDS = {"TRANSIP_PRIVATE_KEY"}  # een PEM plak je niet op één regel


def read_multiline(prompt: str, end_marker: str = "-----END PRIVATE KEY-----") -> str:
    """Plakken van een meerregelige sleutel, tot en met de end-marker."""
    print(prompt)
    print(f"(plak de hele sleutel; de invoer stopt na de regel {end_marker})")
    lines: list[str] = []
    while True:
        line = input()
        lines.append(line)
        if end_marker in line:
            return "\n".join(lines) + "\n"


def new_value_for(component: Component, field: str, template_fields: dict[str, tuple[str | None, int | None]]) -> str:
    """De nieuwe waarde voor één veld: genereren waar de template dat kan, anders invoeren.

    `external`-componenten vragen áltijd om invoer: de waarde komt van buiten dit cluster.
    """
    kind, length = template_fields.get(field, (None, None))
    if (kind is None or length is None) and component.gen_without_template:
        kind, length = component.gen_without_template
    generatable = component.category != "external" and kind in ("random", "bcrypt")
    if generatable:
        answer = input(f"  {field}: opnieuw genereren ({kind}:{length})? [J/n] ").strip().lower()
        if answer not in ("n", "nee"):
            return generate(str(kind), length)
    if field in MULTILINE_FIELDS:
        return read_multiline(f"  nieuwe waarde voor {field}:")
    while True:
        value = input(f"  nieuwe waarde voor {field}: ").strip()
        if value:
            return value
        print("  Leeg is geen wachtwoord.")


def template_fields_of(component: Component) -> dict[str, tuple[str | None, int | None]]:
    """veldnaam -> (kind, length) uit de template-annotaties; leeg voor env-only componenten
    en voor secrets zonder template (die roteren op hun bestaande veldnamen)."""
    path = template_path_of(component) if component.template else None
    if path is None:
        return {}
    return {f.name: (f.kind, f.length) for f in fields_of(path.read_text(encoding="utf-8"))}


def gather_ctx(component: Component, cluster: Cluster, kube: Kube, *, apply_changes: bool) -> Ctx:
    """Lees de huidige waarden en vraag de nieuwe; bouwt nog GEEN stappen."""
    if component.template:
        current = read_current(component, cluster)
    else:
        values = read_env_file(env_file_for(cluster.name))
        if not values:
            raise EditFailed(f"{env_file_for(cluster.name)} niet gevonden of leeg op deze machine")
        component_field = component.zad_env[0] if component.zad_env else component.rotate_fields[0]
        if component_field not in values:
            raise EditFailed(f"{component_field} staat niet in {env_file_for(cluster.name)}")
        # De omgeving noemt dezelfde waarde anders (env-naam ↔ veldnaam). Breng hem naar de
        # veldnaam, zodat stappen overal hetzelfde adresseren: current['root-password'] is
        # dan de huidige BACKUP_S3_SECRET_KEY.
        current = dict(values)
        for field, env_name in zip(component.rotate_fields, component.zad_env, strict=False):
            if field in values:
                continue
            if env_name in values:
                current[field] = values[env_name]

    new: dict[str, str] = {}
    fields = template_fields_of(component)
    print(f"\nNieuwe waarden voor {component.title}:")
    for name in component.rotate_fields:
        new[name] = new_value_for(component, name, fields)

    return Ctx(
        cluster=cluster,
        kube=kube,
        current=current,
        new=new,
        apply_changes=apply_changes,
        rotating=component.rotate_fields[0],
    )


def gather_dry_ctx(component: Component, cluster: Cluster, kube: Kube) -> Ctx:
    """Een context voor --dry-run: de huidige waarden wél gelezen (zodat namen en gebruikers
    goed in het plan staan), de nieuwe als placeholder, en nooit een vraag stellen."""
    current: dict[str, str] = {}
    try:
        current = read_current(component, cluster) if component.template else read_env_file(env_file_for(cluster.name))
    except EditFailed as failure:
        print(f"(let op: {failure})")
    new = dict.fromkeys(component.rotate_fields, "<NIEUW>")
    return Ctx(
        cluster=cluster, kube=kube, current=current, new=new, apply_changes=False, rotating=component.rotate_fields[0]
    )


def check_component(component: Component, cluster: Cluster, kube: Kube) -> list[str]:
    """--check: voer alleen de check-fase uit. Lees-only; meet drift tussen bestand en app."""
    ctx = Ctx(cluster=cluster, kube=kube, current={}, new={}, apply_changes=False, rotating=component.rotate_fields[0])
    if component.template:
        try:
            ctx.current = read_current(component, cluster)
        except EditFailed as failure:
            return [f"  -  {component.key}: overgeslagen ({failure})"]
    else:
        current = read_env_file(env_file_for(cluster.name))
        if component.rotate_fields[0] not in current:
            return [
                f"  -  {component.key}: overgeslagen ({env_file_for(cluster.name).name} bevat geen {component.rotate_fields[0]})"
            ]
        ctx.current = current
    steps = [step for step in steps_for(component, cluster, kube, ctx) if step.phase == "check" and step.run]
    report: list[str] = []
    for step in steps:
        try:
            outcome = step.run(ctx) if step.run else "geen actie"
            report.append(f"  ✅ {component.key}: {outcome}")
        except EditFailed as failure:
            report.append(f"  ❌ {component.key}: DRIFT - {failure}")
        except Exception as failure:
            report.append(
                f"  🚧 {component.key}: de check zelf strandde ({type(failure).__name__}: {str(failure)[:150]})"
            )
    if not steps:
        report.append(f"  ⏭  {component.key}: geen check-stap (categorie {component.category})")
    report.extend(workload_report(component, cluster, kube))
    return report


def workload_report(component: Component, cluster: Cluster, kube: Kube) -> list[str]:
    """Bestaat elke workload die na de rotatie herstart moet worden?

    Informatief, nooit blokkerend: een wachtwoord vervangen mag ook als de consumer hier niet
    draait. Maar een naam die niet bestaat is wel iets om vóór de ronde te weten, want
    `restart_step` slaat die over en dan houdt een draaiende pod het oude geheim in zijn env.
    Zo bleef `prometheus` achter op `deploy/prometheus-server`, een naam die niet bestaat, en
    omdat dat component geen app-verificatie heeft viel het niet op (odcn, 07-10-2026).

    Alleen een NotFound van de API betekent hier "bestaat niet". Elke andere fout betekent
    dat de vraag niet gesteld kon worden: een verlopen token meldde anders van elke workload
    dat hij ontbrak, deploy/operations-manager incluis, en dat is afwezigheid verwarren met
    niet-gemeten (odcn, 09-10-2026).
    """
    lines: list[str] = []
    for workload in component.workloads:
        namespace, name = target_of(workload, cluster)
        namespace = namespace or kube.namespace
        result = kube.run(["get", name, "-n", namespace], check=False)
        detail = (result.stderr or result.stdout).strip()
        if result.returncode == 0:
            lines.append(f"  ✅ {component.key}: {name} bestaat in {namespace}")
        elif "NotFound" in detail or "not found" in detail:
            lines.append(
                f"  -  {component.key}: {name} bestaat niet in {namespace}, dus na de rotatie herstart die niet"
            )
        else:
            lines.append(f"  🚧 {component.key}: kon {name} in {namespace} niet opvragen: {detail[:110]}")
    return lines


def execute(ctx: Ctx, steps: list[Step]) -> bool:
    """Voer het plan uit, per stap een bevestiging. Bij een gefaalde check mag de
    gebruiker het HUIDIGE wachtwoord invoeren (drift) en probeert de check opnieuw."""
    for step in steps:
        print(f"\n[{step.phase}] {step.title}")
        if step.run is None:
            for line in step.dry_text:
                print(f"    {line}")
            if step.phase == "manual":
                input("    Zet dit klaar en druk op enter om door te gaan...")
            continue
        # Default JA: dit is een rotatieronde waar je in één keer door wil, en elke stap
        # bewijst zichzelf of faalt hard. Alleen een expliciete 'n' slaat over.
        if input("    Uitvoeren? [J/n] ").strip().lower() in ("n", "nee", "no"):
            print("    Overgeslagen.")
            continue
        try:
            print(f"    {step.run(ctx)}")
        except EditFailed as failure:
            if step.phase == "check":
                print(f"    ❌ {failure}")
                value = input("    Bestand en app lopen uiteen. Huidige app-wachtwoord: ").strip()
                if not value:
                    return False
                ctx.current[ctx.rotating] = value
                print(f"    {step.run(ctx)}")  # opnieuw met ingevoerde waarde
            else:
                raise
    return True
