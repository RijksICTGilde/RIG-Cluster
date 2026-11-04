#!/usr/bin/env python3
"""Meet waar de tijd van een rollout-taak heen gaat, op een echte sandbox.

Een ZAD-taak met rollout duurt tientallen seconden terwijl de ArgoCD-sync erin een paar
seconden kost (RC-418). Deze bank meet dat per scenario: de wandkloktijd van de taak, de
fases uit het histogram ``opi_rollout_phase_seconds``, en daaruit de tijd die ZAD zelf
toevoegt (taakduur min de wacht op ArgoCD). Zo toont elke stap uit het plan zijn eigen
winst, en een stap die niets oplevert valt op.

De scenario's, met de letters uit het plan:

* ``a1`` -- een nieuwe deployment in een project met 1 bestaande deployment
* ``a5`` -- dezelfde handeling in een project met 5 bestaande deployments
* ``b``  -- een image-update op een bestaande deployment
* ``c``  -- ``configure_service`` met een deployment in de payload

Scenario ``d`` (een nieuw project) staat hier niet: ``POST /api/v2/projects`` vraagt een
SSO-bearertoken en de sandbox heeft geen SSO-Rijk, dus die meting hoort bij een mens met
een browser. Zie het verslag op de PR.

Alles loopt over de API van de Operations Manager, dus de bank meet de echte weg.

Omgeving (dezelfde variabelen als scripts/sandbox_project_tool.py):
    ZAD_BASE_URL     default https://zad.sandbox.rijksapp.dev
    ZAD_SECRET_KEY of E2E_SECRET_KEY: de SECRET_KEY van het cluster
    ZAD_USER_EMAIL   default admin@sandbox.rijksapp.dev (beheerder van het project)
    ZAD_METRICS_TOKEN: PROMETHEUS_METRICS_AUTH_TOKEN van het cluster, voor /metrics

Gebruik (uit operations-manager/python):

    uv run python scripts/bench_rollout.py --project mijnproject
    uv run python scripts/bench_rollout.py --project mijnproject --scenarios a1,b
    uv run python scripts/bench_rollout.py --project mijnproject --label na-deel-1 \
        --json /tmp/bench-na.json

De bank laat het project achter zoals ze het vond: de deployments die ze aanmaakte ruimt
ze weer op. Met ``--keep`` blijven ze staan, zodat een tweede gang (voor en na) dezelfde
deployments gebruikt in plaats van ze opnieuw te maken.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Python zet bij een aanroep met een BESTANDSNAAM de map van het SCRIPT op ``sys.path`` en
# niet de projectwortel, en dan is ``tests`` niet te importeren.
PYTHON_ROOT = Path(__file__).resolve().parents[1]
if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

from opi.core.metrics import (  # noqa: E402
    PHASE_ARGO_WAIT_HEALTHY,
    PHASE_ARGO_WAIT_SYNCED,
    ROLLOUT_PHASES,
)
from scripts.sandbox_project_tool import _CTX, BASE, get_api_key  # noqa: E402

#: De twee fases die op ArgoCD en de pod wachten. Doel 2 van het plan is de taakduur MIN
#: deze twee: dat is de tijd die ZAD zelf toevoegt, en het enige dat dit plan kan korten.
WACHTFASES = (PHASE_ARGO_WAIT_SYNCED, PHASE_ARGO_WAIT_HEALTHY)


_SAMPLE = re.compile(r"^opi_rollout_phase_seconds_(sum|count)\{([^}]*)\}\s+([0-9.eE+-]+)$")


def _label(labels: str, naam: str) -> str:
    treffer = re.search(rf'{naam}="([^"]*)"', labels)
    return treffer.group(1) if treffer else ""


@dataclass
class FaseMeting:
    """De fases van een taak: per fase de som en het aantal observaties."""

    som: dict[str, float] = field(default_factory=dict)
    aantal: dict[str, float] = field(default_factory=dict)

    def wachttijd(self) -> float:
        return sum(self.som.get(fase, 0.0) for fase in WACHTFASES)


def _request(path: str, *, method: str = "GET", api_key: str | None = None, body: Any = None) -> tuple[int, str]:
    """Doe een API-verzoek met de projectsleutel, of met de beheerderssessie zonder sleutel."""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method, headers=headers)  # noqa: S310
    try:
        with urllib.request.urlopen(req, context=_CTX, timeout=180) as resp:  # noqa: S310
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def _scrape() -> dict[str, FaseMeting]:
    """De fase-histogrammen per taaktype, zoals /metrics ze nu meldt."""
    token = os.environ.get("ZAD_METRICS_TOKEN", "")
    if not token:
        raise SystemExit("geen ZAD_METRICS_TOKEN: zonder dat token geeft /metrics een 401")
    req = urllib.request.Request(  # noqa: S310
        f"{BASE}/metrics", headers={"Authorization": f"Bearer {token}"}
    )
    with urllib.request.urlopen(req, context=_CTX, timeout=60) as resp:  # noqa: S310
        tekst = resp.read().decode()

    per_taaktype: dict[str, FaseMeting] = {}
    for regel in tekst.splitlines():
        treffer = _SAMPLE.match(regel)
        if not treffer:
            continue
        soort, labels, waarde = treffer.groups()
        meting = per_taaktype.setdefault(_label(labels, "task_type"), FaseMeting())
        doel = meting.som if soort == "sum" else meting.aantal
        doel[_label(labels, "phase")] = float(waarde)
    return per_taaktype


def _diff(voor: dict[str, FaseMeting], na: dict[str, FaseMeting], taaktype: str) -> FaseMeting:
    """Wat er tussen twee scrapes bij kwam voor dit taaktype."""
    a, b = voor.get(taaktype, FaseMeting()), na.get(taaktype, FaseMeting())
    uitkomst = FaseMeting()
    for fase in ROLLOUT_PHASES:
        som = b.som.get(fase, 0.0) - a.som.get(fase, 0.0)
        aantal = b.aantal.get(fase, 0.0) - a.aantal.get(fase, 0.0)
        if aantal > 0:
            uitkomst.som[fase] = som
            uitkomst.aantal[fase] = aantal
    return uitkomst


def _task_id(tekst: str) -> str:
    gegevens = json.loads(tekst)
    task_id = gegevens.get("task_id") or (gegevens.get("task") or {}).get("task_id")
    if not task_id:
        raise SystemExit(f"geen task_id in het antwoord: {tekst[:300]}")
    return str(task_id)


def _wacht(task_id: str, api_key: str, timeout: float = 600.0) -> str:
    """Peil de taak tot hij klaar is en geef zijn eindstatus."""
    einde = time.monotonic() + timeout
    while time.monotonic() < einde:
        status, tekst = _request(f"/api/tasks/{task_id}", api_key=api_key)
        if status == 200:
            return str(json.loads(tekst).get("status", "completed"))
        if status != 202:
            return f"peilen gaf HTTP {status}: {tekst[:200]}"
        time.sleep(1.0)
    return "timeout"


@dataclass
class Uitkomst:
    """Wat een scenario opleverde."""

    scenario: str
    taaktype: str
    status: str
    duur: float
    fases: FaseMeting

    @property
    def zad_tijd(self) -> float:
        """De taakduur min de wacht op ArgoCD en de pod: de tijd die ZAD zelf toevoegt."""
        return max(self.duur - self.fases.wachttijd(), 0.0)


def _meet(scenario: str, taaktype: str, api_key: str, start: tuple[str, str, Any]) -> Uitkomst:
    """Start een taak, wacht hem af en reken uit hoe lang elke fase duurde."""
    method, path, body = start
    voor = _scrape()
    begonnen = time.monotonic()
    status, tekst = _request(path, method=method, api_key=api_key, body=body)
    if status != 202:
        return Uitkomst(scenario, taaktype, f"HTTP {status}: {tekst[:200]}", 0.0, FaseMeting())
    uitslag = _wacht(_task_id(tekst), api_key)
    duur = time.monotonic() - begonnen
    return Uitkomst(scenario, taaktype, uitslag, duur, _diff(voor, _scrape(), taaktype))


def _deployments(project: str, api_key: str) -> list[str]:
    status, tekst = _request(f"/api/v2/projects/{project}", api_key=api_key)
    if status != 200:
        raise SystemExit(f"kon project '{project}' niet lezen: HTTP {status}: {tekst[:200]}")
    gegevens = json.loads(tekst)
    return [str(d.get("name")) for d in gegevens.get("deployments") or [] if d.get("name")]


def _deployment_dienstconfig(project: str, api_key: str) -> tuple[str, str, Any] | None:
    """Een dienst met een config op een DEPLOYMENT: de dienst, die deployment en de config.

    Scenario c schrijft diezelfde config terug: dan verandert er niets aan de uitrol en
    meet de gang alleen wat de taak eromheen kost. Elke deployment komt in aanmerking, en
    niet alleen de deployment die de bank zelf aanmaakte: die heeft per definitie nog geen
    config op zijn eigen laag.
    """
    status, tekst = _request(f"/api/v2/projects/{project}/services", api_key=api_key)
    if status != 200:
        return None
    for dienst in json.loads(tekst).get("services") or []:
        naam = dienst if isinstance(dienst, str) else str(dienst.get("name", ""))
        if not naam:
            continue
        status, tekst = _request(f"/api/v2/projects/{project}/services/{naam}/config", api_key=api_key)
        if status != 200:
            continue
        for gevonden in json.loads(tekst).get("configurations") or []:
            if gevonden.get("target") == "deployment" and gevonden.get("deployment"):
                return naam, str(gevonden["deployment"]), gevonden.get("config")
    return None


def _tabel(uitkomsten: list[Uitkomst]) -> str:
    """De uitkomst als tabel per scenario, met de fases eronder."""
    regels = [
        "| scenario | taaktype | status | taakduur | wacht op ArgoCD | ZAD-tijd |",
        "|---|---|---|---|---|---|",
    ]
    regels.extend(
        f"| {u.scenario} | {u.taaktype} | {u.status} | {u.duur:.1f} s | "
        f"{u.fases.wachttijd():.1f} s | {u.zad_tijd:.1f} s |"
        for u in uitkomsten
    )
    regels.append("")
    regels.append("| scenario | fase | aantal | totaal |")
    regels.append("|---|---|---|---|")
    for u in uitkomsten:
        regels.extend(
            f"| {u.scenario} | {fase} | {u.fases.aantal[fase]:.0f} | {u.fases.som[fase]:.1f} s |"
            for fase in ROLLOUT_PHASES
            if fase in u.fases.som
        )
    return "\n".join(regels)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", required=True, help="bestaand sandboxproject om op te meten")
    parser.add_argument("--scenarios", default="a1,a5,b,c", help="komma-lijst: a1,a5,b,c")
    parser.add_argument("--label", default="", help="naam van deze gang, voor het verslag")
    parser.add_argument("--json", dest="json_pad", default="", help="schrijf de rauwe uitkomst hierheen")
    parser.add_argument("--keep", action="store_true", help="laat de aangemaakte deployments staan")
    parser.add_argument(
        "--alt-image",
        default="",
        help=(
            "het image waar scenario b naartoe gaat. Zonder dit wordt het huidige image met "
            "zijn digest gepind, wat hetzelfde image is maar een andere verwijzing: de uitrol "
            "loopt dan echt en de pod start uit de cache."
        ),
    )
    args = parser.parse_args(argv[1:])

    project = args.project
    gevraagd = [s.strip() for s in args.scenarios.split(",") if s.strip()]
    api_key = get_api_key(project)
    bestaand = _deployments(project, api_key)
    print(f"project '{project}' heeft {len(bestaand)} deployment(s): {', '.join(bestaand) or 'geen'}")

    uitkomsten: list[Uitkomst] = []
    aangemaakt: list[str] = []

    def voeg_deployment_toe(naam: str, image: str) -> tuple[str, str, Any]:
        return (
            "POST",
            f"/api/v2/projects/{project}/:upsert-deployment",
            {"deploymentName": naam, "components": [{"reference": component, "image": image}]},
        )

    component, image = _component_en_image(project, api_key)
    print(f"component '{component}' op image '{image}'")

    # Scenario a draait op een project dat eerst 1 en daarna 5 deployments heeft. De bank
    # vult aan tot dat aantal met deployments die ze zelf opruimt.
    for scenario, nodig in (("a1", 1), ("a5", 5)):
        if scenario not in gevraagd:
            continue
        huidig = _deployments(project, api_key)
        while len(huidig) < nodig:
            vul = f"bench-vul-{len(huidig) + 1}"
            print(f"  aanvullen tot {nodig}: {vul}")
            vulling = _meet(f"{scenario}-vulling", "upsert_deployment", api_key, voeg_deployment_toe(vul, image))
            if vulling.status != "completed":
                print(f"  LET OP: aanvullen met {vul} gaf '{vulling.status}'")
            aangemaakt.append(vul)
            huidig = _deployments(project, api_key)
        nieuw = f"bench-{scenario}"
        uitkomsten.append(_meet(scenario, "upsert_deployment", api_key, voeg_deployment_toe(nieuw, image)))
        aangemaakt.append(nieuw)

    if "b" in gevraagd:
        doel = (aangemaakt or _deployments(project, api_key))[0]
        alt = args.alt_image or _gepinde_verwijzing(image)
        if not alt:
            print("  scenario b overgeslagen: geen tweede imageverwijzing (geef --alt-image)")
            gevraagd = [s for s in gevraagd if s != "b"]
    if "b" in gevraagd:
        uitkomsten.append(
            _meet(
                "b",
                "update_image",
                api_key,
                (
                    "PUT",
                    f"/api/v2/projects/{project}/deployments/{doel}/image",
                    {"componentName": component, "newImageUrl": alt},
                ),
            )
        )

    if "c" in gevraagd:
        dienst = _deployment_dienstconfig(project, api_key)
        if dienst is None:
            print("  scenario c overgeslagen: geen dienst met een config op een deployment")
        else:
            naam, doel, config = dienst
            uitkomsten.append(
                _meet(
                    "c",
                    "configure_service",
                    api_key,
                    ("PUT", f"/api/v2/projects/{project}/services/{naam}/config/deployment/{doel}", config),
                )
            )

    if aangemaakt and not args.keep:
        for naam in dict.fromkeys(reversed(aangemaakt)):
            status, tekst = _request(f"/api/v2/projects/{project}/{naam}", method="DELETE", api_key=api_key)
            print(f"  opruimen {naam} -> HTTP {status}")
            if status == 202:
                _wacht(_task_id(tekst), api_key)

    kop = f"# Rollout-meting {args.label or 'zonder label'} op '{project}'"
    print("\n" + kop + "\n")
    print(_tabel(uitkomsten))

    if args.json_pad:
        Path(args.json_pad).write_text(
            json.dumps(
                {
                    "label": args.label,
                    "project": project,
                    "uitkomsten": [
                        {
                            "scenario": u.scenario,
                            "task_type": u.taaktype,
                            "status": u.status,
                            "duur": u.duur,
                            "wacht_op_argocd": u.fases.wachttijd(),
                            "zad_tijd": u.zad_tijd,
                            "fases": {f: {"som": u.fases.som[f], "aantal": u.fases.aantal[f]} for f in u.fases.som},
                        }
                        for u in uitkomsten
                    ],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nrauwe uitkomst in {args.json_pad}")

    return 0 if all(u.status == "completed" for u in uitkomsten) else 1


#: De mediatypes die een registry mag teruggeven voor een manifest; zonder deze header
#: antwoordt ghcr met het oude v1-formaat en een andere digest.
_MANIFEST_ACCEPT = (
    "application/vnd.oci.image.index.v1+json, "
    "application/vnd.oci.image.manifest.v1+json, "
    "application/vnd.docker.distribution.manifest.list.v2+json, "
    "application/vnd.docker.distribution.manifest.v2+json"
)


def _gepinde_verwijzing(image: str) -> str:
    """Hetzelfde image, maar met zijn digest erbij: een andere verwijzing, dezelfde bytes.

    Zo verandert scenario b echt iets in het projectbestand (en loopt de hele uitrol) zonder
    dat er een image bij komt die de node nog moet pullen. Een verwijzing die al een digest
    draagt levert niets op, en dan slaat de bank het scenario over.
    """
    if "@" in image or ":" not in image.rsplit("/", 1)[-1]:
        return ""
    repository = image.rsplit(":", 1)[0]
    digest = _digest_van(repository, image.rsplit(":", 1)[1])
    return f"{repository}@{digest}" if digest else ""


def _digest_van(repository: str, tag: str) -> str:
    """De digest van een tag, bij de registry opgevraagd. Lege string als dat niet lukt."""
    if not repository.startswith("ghcr.io/"):
        return ""
    pad = repository[len("ghcr.io/") :]
    try:
        with urllib.request.urlopen(
            f"https://ghcr.io/token?scope=repository:{pad}:pull&service=ghcr.io", timeout=30
        ) as antwoord:
            token = json.load(antwoord)["token"]
        verzoek = urllib.request.Request(
            f"https://ghcr.io/v2/{pad}/manifests/{tag}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": _MANIFEST_ACCEPT,
            },
        )
        with urllib.request.urlopen(verzoek, timeout=30) as antwoord:  # noqa: S310
            return antwoord.headers.get("Docker-Content-Digest", "")
    except (urllib.error.URLError, OSError, KeyError, ValueError) as fout:
        print(f"  kon de digest van {repository}:{tag} niet opvragen: {fout}")
        return ""


def _component_en_image(project: str, api_key: str) -> tuple[str, str]:
    """Het eerste component van het project en het image dat het nu draait.

    Het bestaande image en niet een eigen keuze: dat staat al op de node, dus de pod-start
    domineert de meting niet, en een image dat de sandbox niet kan pullen zou elk scenario
    op een ImagePullBackOff laten stranden in plaats van iets te meten.
    """
    status, tekst = _request(f"/api/v2/projects/{project}", api_key=api_key)
    if status != 200:
        raise SystemExit(f"kon project '{project}' niet lezen: HTTP {status}: {tekst[:200]}")
    gegevens = json.loads(tekst)
    for deployment in gegevens.get("deployments") or []:
        for component in deployment.get("components") or []:
            if component.get("reference") and component.get("image"):
                return str(component["reference"]), str(component["image"])
    raise SystemExit(f"project '{project}' heeft geen deployment met een component en een image")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
