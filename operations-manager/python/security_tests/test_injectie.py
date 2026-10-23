"""Invoer met shell-metatekens of een pad-escape komt niet op een argumentenlijst terecht.

OPI roept kubectl, sops, git, mc, kopia en skopeo als subprocess aan, en invoer uit een
projectbestand of uit de API loopt daar doorheen. In de proefsandbox zijn die binaries
nagebootst (``security-proof/bin``): ze schrijven hun argumentenlijst weg en doen niets. Deze
proef stuurt payloads naar binnen en leest dat logboek.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

import pytest

from security_tests.conftest import aanroepen, sleutelclient

if TYPE_CHECKING:
    from security_tests.doel import Doel

#: Komt in elke payload voor, zodat in een argumentenlijst te zien is waar hij vandaan komt.
MARKER = "zadproef"

#: Wat een payload probeert: een commando erbij, een variabele, een pad-escape, een extra vlag.
PAYLOADS = (
    f"{MARKER};id",
    f"{MARKER}$(id)",
    f"{MARKER}`id`",
    f"{MARKER}|id",
    f"../../etc/{MARKER}",
    f"--kubeconfig=/tmp/{MARKER}",
    f"{MARKER}\nid",
)

#: De fragmenten die een payload gevaarlijk maken zodra hij op een argumentenlijst staat.
GEVAARLIJK = (";", "$(", "`", "|", "&&", "\n", "../", "--kubeconfig")


class Aanval(NamedTuple):
    """Een veld waar invoer naar binnen gaat, met de vorm van het verzoek erom heen."""

    methode: str
    pad: str
    veld: str
    basis: dict[str, Any]


def aanvallen(project: str) -> list[Aanval]:
    """De velden die deze proef met een payload vult."""
    return [
        Aanval("POST", f"/api/v2/projects/{project}/components", "name", {"image": "nginx"}),
        Aanval("POST", f"/api/v2/projects/{project}/components", "image", {"name": "proefcomponent"}),
        Aanval(
            "PUT",
            f"/api/v2/projects/{project}/deployments/deployment-1/image",
            "newImageUrl",
            {"componentName": "component-1"},
        ),
        Aanval("POST", f"/api/v2/projects/{project}/services", "service_name", {}),
    ]


class Uitslag(NamedTuple):
    """Wat er van een ronde payloads terugkwam, en wat de binaries ervan zagen."""

    statussen: dict[str, int]
    nieuwe_aanroepen: list[dict]


def beoordeel_status(status: int) -> str | None:
    """De melding als dit antwoord op een payload niet goed is.

    Een 4xx is de weigering. Een 2xx betekent dat de payload is overgenomen, een 5xx dat hij
    de applicatie heeft geraakt in plaats van de validatie.
    """
    if 400 <= status < 500:
        return None
    if status >= 500:
        return f"5xx ({status}) op een payload"
    return f"payload overgenomen met {status}"


def beoordeel_aanroepen(regels: list[dict]) -> list[str]:
    """De aanroepen waarin een payload of een pad-escape op de argumentenlijst staat."""
    bevindingen = []
    for regel in regels:
        for argument in regel.get("argv", []):
            if MARKER in argument and any(fragment in argument for fragment in GEVAARLIJK):
                bevindingen.append(f"{regel['tool']} kreeg {argument!r}")
            elif ".." in Path(argument).parts:
                bevindingen.append(f"{regel['tool']} kreeg een pad buiten zijn map: {argument!r}")
    return bevindingen


@pytest.fixture(scope="module")
def ronde(doel: Doel) -> Uitslag:
    """Stuur elke payload naar elk veld, en houd bij wat de binaries daarna zagen."""
    project = doel.projecten[0]
    voor = len(aanroepen(doel.aanroeplog))
    statussen: dict[str, int] = {}
    with sleutelclient(doel, project) as client:
        for aanval in aanvallen(project):
            for payload in PAYLOADS:
                antwoord = client.request(aanval.methode, aanval.pad, json={**aanval.basis, aanval.veld: payload})
                statussen[f"{aanval.methode} {aanval.pad} [{aanval.veld}={payload!r}]"] = antwoord.status_code
    assert statussen, "geen enkele payload is verstuurd; de proef zou stil groen staan"
    return Uitslag(statussen, aanroepen(doel.aanroeplog)[voor:])


def test_een_payload_wordt_geweigerd(ronde: Uitslag) -> None:
    """Faalconditie: een payload wordt overgenomen (2xx) of raakt de applicatie (5xx)."""
    bevindingen = [
        f"{beschrijving}: {beoordeel_status(status)}"
        for beschrijving, status in ronde.statussen.items()
        if beoordeel_status(status)
    ]
    assert not bevindingen, "\n".join(sorted(bevindingen))


def test_geen_payload_bereikt_een_argumentenlijst(ronde: Uitslag) -> None:
    """Faalconditie: een metateken of pad-escape staat op de argumentenlijst van een subprocess."""
    bevindingen = beoordeel_aanroepen(ronde.nieuwe_aanroepen)
    assert not bevindingen, "\n".join(bevindingen)


def test_de_injectieproef_merkt_een_doorgelaten_payload_op() -> None:
    """Negatief voorbeeld: zo ziet een aanroep eruit die de proef rood hoort te maken."""
    assert beoordeel_aanroepen([{"tool": "kubectl", "argv": ["get", f"ns/{MARKER};id"]}]) == [
        f"kubectl kreeg 'ns/{MARKER};id'"
    ]
    assert beoordeel_aanroepen([{"tool": "sops", "argv": ["-d", "../../security/key.txt"]}]) == [
        "sops kreeg een pad buiten zijn map: '../../security/key.txt'"
    ]
    assert beoordeel_aanroepen([{"tool": "git", "argv": ["clone", "https://host/repo.git"]}]) == []
    assert beoordeel_status(201) == "payload overgenomen met 201"
    assert beoordeel_status(400) is None
