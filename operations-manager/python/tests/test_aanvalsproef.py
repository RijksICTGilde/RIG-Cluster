"""De gates van de aanvalsproef: de grendel op het doelwit en de uitzonderingslijst.

Twee dingen moeten vaststaan. Een scanner vuurt alleen op een doelwit dat de run zelf heeft
opgezet, nooit op productie of op de gedeelde sandbox. En een uitzondering zwijgt alleen met
een reden en een vervaldatum, zodat de bevinding vanzelf terugkomt. Zie
features/aanvalsproef-harnas.md.
"""

import json
import re
import tomllib
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from opi.tools.aanvalsproef import (
    LEZERS,
    UITZONDERINGEN,
    Bevinding,
    Grendelfout,
    boven_drempel,
    gate,
    geldige_uitzonderingen,
    grendel,
    lees_checkov,
    lees_kube_linter,
    lees_nuclei,
    lees_semgrep,
    main,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "aanvalsproef.yml"
GITLEAKS_CONFIG = REPO_ROOT / "aanvalsproef" / "gitleaks.toml"
SECURITY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "security.yml"

#: Een uitzondering op een posture- of scankeuze mag maximaal een jaar staan. Ruimer dan de
#: 90 dagen van .trivyignore.yaml, omdat het hier om een structurele keuze gaat en niet om een
#: kwetsbaarheid die op een patch wacht.
MAX_GELDIGHEID = timedelta(days=365)


@pytest.mark.parametrize(
    "doelwit",
    [
        "http://127.0.0.1:8111",
        "http://localhost:8111/",
        "https://127.0.0.1",
        "http://[::1]:8000",
        "http://127.0.0.2:8111",
    ],
)
def test_de_grendel_laat_een_eigen_doelwit_door(doelwit: str) -> None:
    grendel(doelwit)


@pytest.mark.parametrize(
    "doelwit",
    [
        "https://zad.rijksapps.nl",
        "https://zad.sandbox.rijksapp.dev",
        "http://10.0.0.1",
        "ftp://127.0.0.1",
        "127.0.0.1:8111",
        "http://",
    ],
)
def test_de_grendel_weigert_alles_wat_geen_loopback_is(doelwit: str) -> None:
    with pytest.raises(Grendelfout):
        grendel(doelwit)


def test_de_lezers_dekken_elke_sectie_van_de_uitzonderingslijst() -> None:
    """Een sectie zonder lezer is een lijst die niets stil zet."""
    secties = set(yaml.safe_load(UITZONDERINGEN.read_text(encoding="utf-8")) or {})
    assert secties <= set(LEZERS), f"secties zonder lezer: {secties - set(LEZERS)}"


def _uitzonderingen() -> list[tuple[str, dict[str, Any]]]:
    inhoud = yaml.safe_load(UITZONDERINGEN.read_text(encoding="utf-8")) or {}
    return [(soort, regel) for soort, regels in inhoud.items() for regel in (regels or [])]


def test_elke_uitzondering_heeft_een_reden_en_verloopt() -> None:
    vandaag = datetime.now(tz=UTC).date()
    fouten = []
    for soort, regel in _uitzonderingen():
        naam = f"{soort}/{regel.get('id', '<zonder id>')}"
        if not str(regel.get("statement") or "").strip():
            fouten.append(f"{naam}: geen statement, dus geen vastgelegde reden")
        vervalt = regel.get("expired_at")
        if not isinstance(vervalt, date):
            fouten.append(f"{naam}: geen expired_at als datum, gevonden: {vervalt!r}")
        elif vervalt - vandaag > MAX_GELDIGHEID:
            fouten.append(f"{naam}: expired_at {vervalt} ligt verder dan {MAX_GELDIGHEID.days} dagen vooruit")
    assert not fouten, "uitzonderingen zonder reden of met een te ruime vervaldatum:\n" + "\n".join(fouten)


def test_een_verlopen_uitzondering_zwijgt_niet_meer(tmp_path: Path) -> None:
    pad = tmp_path / "uitzonderingen.yaml"
    pad.write_text(
        yaml.safe_dump(
            {
                "nuclei": [
                    {"id": "nog-geldig", "statement": "reden", "expired_at": date(2099, 1, 1)},
                    {"id": "verlopen", "statement": "reden", "expired_at": date(2020, 1, 1)},
                    {"id": "zonder-reden", "statement": "  ", "expired_at": date(2099, 1, 1)},
                ]
            }
        ),
        encoding="utf-8",
    )
    assert geldige_uitzonderingen("nuclei", date(2026, 1, 1), pad) == {"nog-geldig"}


def _reden_en_vervaldatum(blok: str, naam: str, vandaag: date) -> list[str]:
    """De twee eigenschappen die elke uitzondering draagt, waar hij ook staat: TOML of YAML."""
    fouten = []
    if not re.search(r"^\s*#\s*reden:", blok, re.MULTILINE):
        fouten.append(f"{naam}: geen reden in het blok")
    datum = re.search(r"^\s*#\s*vervalt:\s*(\d{4}-\d{2}-\d{2})\s*$", blok, re.MULTILINE)
    if not datum:
        return [*fouten, f"{naam}: geen vervaldatum in het blok"]
    vervalt = date.fromisoformat(datum.group(1))
    if vervalt < vandaag:
        fouten.append(f"{naam}: vervallen op {vervalt}, de uitzondering zwijgt niet meer")
    elif vervalt - vandaag > MAX_GELDIGHEID:
        fouten.append(f"{naam}: vervalt {vervalt}, verder dan {MAX_GELDIGHEID.days} dagen vooruit")
    return fouten


def test_elke_gitleaks_allowlist_heeft_een_reden_en_verloopt() -> None:
    """De gitleaks-kant heeft dezelfde eigenschap als uitzonderingen.yaml, alleen in TOML.

    gitleaks kent zelf geen vervaldatum, dus de reden en de datum staan als commentaar in het
    blok zelf en deze toets is wat ze laat verlopen.
    """
    tekst = GITLEAKS_CONFIG.read_text(encoding="utf-8")
    blokken = tekst.split("[[allowlists]]")[1:]
    assert len(blokken) == len(tomllib.loads(tekst)["allowlists"]), (
        "een allowlist-blok wordt anders gesplitst dan gitleaks hem leest"
    )

    vandaag = datetime.now(tz=UTC).date()
    fouten = []
    for blok in blokken:
        naam = re.search(r'description\s*=\s*"(.*)"', blok)
        fouten += _reden_en_vervaldatum(blok, naam.group(1) if naam else "<zonder description>", vandaag)
    assert not fouten, "allowlists in gitleaks.toml zonder reden of vervaldatum:\n" + "\n".join(fouten)


def test_een_aanvaard_advisory_op_een_gepinde_scanner_verloopt() -> None:
    """Een gepinde scanner met een openstaand advisory mag blijven staan, niet stilzwijgend.

    Een advisory waarvoor geen enkele release een fix heeft is niet weg te bumpen. Dan geldt
    dezelfde vorm als in gitleaks.toml en .trivyignore.yaml: een reden en een vervaldatum,
    hier als commentaar boven de pin, zodat de afweging vanzelf terugkomt.
    """
    vandaag = datetime.now(tz=UTC).date()
    fouten = []
    for workflow in (WORKFLOW, SECURITY_WORKFLOW):
        tekst = workflow.read_text(encoding="utf-8")
        blokken = re.findall(r"(?:^[ \t]*#.*\n)+^[ \t]*[A-Z_]+:[ \t]*\S+$", tekst, re.MULTILINE)
        for blok in blokken:
            if "# reden:" not in blok:
                continue
            naam = f"{workflow.name}: {blok.rstrip().rsplit('\n', 1)[-1].strip()}"
            fouten += _reden_en_vervaldatum(blok, naam, vandaag)
    assert not fouten, "aanvaarde advisories zonder reden of vervaldatum:\n" + "\n".join(fouten)


def test_gitleaks_history_draait_wekelijks_en_niet_op_een_pull_request() -> None:
    """Dezelfde eigenschap als secret-scan-history (test_secret_scan.py), nu voor gitleaks.

    De historie verandert niet per pull request en een volledige sweep duurt minuten; zonder
    deze `if` zou gitleaks-history op elke push meedraaien in plaats van alleen wekelijks.
    """
    workflow = yaml.safe_load(SECURITY_WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"]["gitleaks-history"]
    assert job["if"] == "github.event_name == 'schedule' || github.event_name == 'workflow_dispatch'"

    checkout = next(stap for stap in job["steps"] if str(stap.get("uses", "")).startswith("actions/checkout"))
    assert checkout["with"]["fetch-depth"] == 0, "een shallow clone heeft geen historie om te scannen"

    assert "if" not in workflow["jobs"]["gitleaks"], "de boom-scan hoort op elke pull request te draaien"


def _aanvalsproef_custom_manager() -> dict[str, Any]:
    """De customManager uit renovate.json die de scanner-pins in deze twee workflows volgt."""
    inhoud = json.loads((REPO_ROOT / "renovate.json").read_text(encoding="utf-8"))
    kandidaten = [
        beheerder
        for beheerder in inhoud["customManagers"]
        if any("aanvalsproef" in patroon for patroon in beheerder.get("managerFilePatterns", []))
    ]
    assert len(kandidaten) == 1, f"verwacht precies een customManager voor de aanvalsproef-workflows: {kandidaten}"
    return kandidaten[0]


def test_de_renovate_regel_vindt_elke_scannerversie_in_de_workflows() -> None:
    """Een regex die niet matcht, volgt Renovate niets: dezelfde fout als een gemiste imageName.

    renovate.json gebruikt PCRE-achtige named groups (``(?<naam>...)``); re wil ``(?P<naam>...)``.
    """
    beheerder = _aanvalsproef_custom_manager()
    (patroon,) = beheerder["matchStrings"]
    regex = re.compile(re.sub(r"\(\?<(\w+)>", r"(?P<\1>", patroon), re.MULTILINE)

    gevonden: dict[str, str] = {}
    for workflow in (WORKFLOW, SECURITY_WORKFLOW):
        for match in regex.finditer(workflow.read_text(encoding="utf-8")):
            gevonden[match.group("depName")] = match.group("currentValue")

    aanwezig = set(
        re.findall(
            r"depName=(\S+)",
            "\n".join([WORKFLOW.read_text(encoding="utf-8"), SECURITY_WORKFLOW.read_text(encoding="utf-8")]),
        )
    )
    assert gevonden.keys() >= aanwezig, (
        f"renovate-regex matcht niet elke geannoteerde pin: gemist {aanwezig - gevonden.keys()}"
    )
    assert all(waarde[0].isdigit() for waarde in gevonden.values()), gevonden
    assert gevonden["projectdiscovery/nuclei"] == "3.11.1"
    assert gevonden["kyverno/kyverno"] == "1.19.2"
    assert gevonden["gitleaks/gitleaks"] == "8.30.1"
    assert gevonden["checkov"] == "3.3.30"


def test_de_drempel_laat_lagere_ernst_liggen_en_onbekende_ernst_niet() -> None:
    bevindingen = [
        Bevinding(id="laag", plek="a", ernst="low"),
        Bevinding(id="midden", plek="b", ernst="medium"),
        Bevinding(id="onbekend", plek="c", ernst="rood"),
    ]
    assert {b.id for b in boven_drempel(bevindingen, "medium")} == {"midden", "onbekend"}


def test_de_gate_houdt_over_wat_niet_is_uitgezonderd() -> None:
    bevindingen = [
        Bevinding(id="bekend", plek="a", ernst="high"),
        Bevinding(id="nieuw", plek="b", ernst="high"),
    ]
    assert [b.id for b in gate(bevindingen, {"bekend"}, "medium")] == ["nieuw"]


def test_de_nuclei_lezer_leest_jsonl(tmp_path: Path) -> None:
    rapport = tmp_path / "nuclei.jsonl"
    rapport.write_text(
        json.dumps({"template-id": "iets", "info": {"severity": "HIGH"}, "matched-at": "http://127.0.0.1/x"}) + "\n\n",
        encoding="utf-8",
    )
    assert lees_nuclei(rapport) == [Bevinding(id="iets", plek="http://127.0.0.1/x", ernst="high")]


def test_de_kube_linter_lezer_noemt_het_object_en_het_bestand(tmp_path: Path) -> None:
    rapport = tmp_path / "kube-linter.json"
    rapport.write_text(
        json.dumps(
            {
                "Reports": [
                    {
                        "Check": "latest-tag",
                        "Object": {
                            "Metadata": {"FilePath": "manifests/deployment.yaml"},
                            "K8sObject": {"Name": "web", "GroupVersionKind": {"Kind": "Deployment"}},
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    assert lees_kube_linter(rapport) == [
        Bevinding(id="latest-tag", plek="Deployment/web in manifests/deployment.yaml", ernst="high")
    ]


def test_de_checkov_lezer_leest_een_lijst_en_een_object(tmp_path: Path) -> None:
    resultaat = {"results": {"failed_checks": [{"check_id": "CKV_K8S_14", "resource": "Pod.web", "file_path": "/p"}]}}
    als_object = tmp_path / "object.json"
    als_object.write_text(json.dumps(resultaat), encoding="utf-8")
    als_lijst = tmp_path / "lijst.json"
    als_lijst.write_text(json.dumps([resultaat]), encoding="utf-8")
    verwacht = [Bevinding(id="CKV_K8S_14", plek="Pod.web in /p", ernst="high")]
    assert lees_checkov(als_object) == verwacht
    assert lees_checkov(als_lijst) == verwacht


def test_de_semgrep_lezer_legt_de_ernst_op_de_schaal(tmp_path: Path) -> None:
    rapport = tmp_path / "semgrep.json"
    rapport.write_text(
        json.dumps(
            {
                "results": [
                    {
                        "check_id": "regel.een",
                        "path": "opi/web/x.py",
                        "start": {"line": 7},
                        "extra": {"severity": "WARNING"},
                    },
                    {
                        "check_id": "regel.twee",
                        "path": "opi/web/y.py",
                        "start": {"line": 9},
                        "extra": {"severity": "INFO"},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    assert lees_semgrep(rapport) == [
        Bevinding(id="regel.een", plek="opi/web/x.py:7", ernst="medium"),
        Bevinding(id="regel.twee", plek="opi/web/y.py:9", ernst="low"),
    ]


def test_de_cli_meldt_een_ontbrekend_rapport(tmp_path: Path) -> None:
    """Een scanner die niets schreef is geen groene gate."""
    assert main(["nuclei", str(tmp_path / "bestaat-niet.jsonl")]) == 1


def test_de_cli_faalt_op_een_bevinding_boven_de_drempel(tmp_path: Path) -> None:
    rapport = tmp_path / "nuclei.jsonl"
    rapport.write_text(json.dumps({"template-id": "iets", "info": {"severity": "critical"}}) + "\n", encoding="utf-8")
    assert main(["nuclei", str(rapport)]) == 1
    assert main(["nuclei", str(rapport), "--drempel", "critical"]) == 1


def test_de_cli_slaagt_zonder_blokkerende_bevinding(tmp_path: Path) -> None:
    """Een rapport zonder bevindingen boven de drempel is een groene gate."""
    rapport = tmp_path / "nuclei.jsonl"
    rapport.write_text(json.dumps({"template-id": "iets", "info": {"severity": "low"}}) + "\n", encoding="utf-8")
    assert main(["nuclei", str(rapport)]) == 0


def test_de_cli_telt_een_uitgezonderde_bevinding_niet_mee(tmp_path: Path) -> None:
    """Dezelfde bevinding als hierboven, maar nu uitgezonderd: dat laat de run groen."""
    rapport = tmp_path / "nuclei.jsonl"
    rapport.write_text(json.dumps({"template-id": "iets", "info": {"severity": "critical"}}) + "\n", encoding="utf-8")
    uitzonderingen = tmp_path / "uitzonderingen.yaml"
    uitzonderingen.write_text(
        yaml.safe_dump({"nuclei": [{"id": "iets", "statement": "reden", "expired_at": date(2099, 1, 1)}]}),
        encoding="utf-8",
    )
    assert main(["nuclei", str(rapport), "--uitzonderingen", str(uitzonderingen)]) == 0


def test_de_cli_grendelt_het_doelwit() -> None:
    assert main(["grendel", "http://127.0.0.1:8111"]) == 0
    assert main(["grendel", "https://zad.rijksapps.nl"]) == 1


def test_de_workflow_grendelt_voordat_hij_scant() -> None:
    """Zonder die stap kan een verschrijving in het doelwit nog op een echte instantie landen."""
    stappen = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["nuclei"]["steps"]
    namen = [stap.get("name", "") for stap in stappen]
    commandos = [str(stap.get("run", "")) for stap in stappen]
    grendelstap = next(i for i, cmd in enumerate(commandos) if "aanvalsproef grendel" in cmd)
    scanstap = next(i for i, cmd in enumerate(commandos) if "nuclei -u" in cmd)
    assert grendelstap < scanstap, f"de grendel staat na de scan: {namen}"


def test_de_grendel_toetst_hetzelfde_doelwit_als_de_scan() -> None:
    """De grendel leest een eigen string: wijkt die af van `nuclei -u`, dan bewaakt hij niets."""
    stappen = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["nuclei"]["steps"]
    regels = [regel for stap in stappen for regel in str(stap.get("run", "")).splitlines()]
    gegrendeld = {r.split("aanvalsproef grendel", 1)[1].split()[0] for r in regels if "aanvalsproef grendel" in r}
    gescand = {r.split("nuclei -u", 1)[1].split()[0] for r in regels if "nuclei -u" in r}
    assert gescand, "geen scanstap met nuclei -u gevonden"
    assert gescand == gegrendeld, f"gescand wordt {gescand}, maar de grendel toetste {gegrendeld}"


def test_elke_gate_in_de_workflows_gebruikt_een_lezer_die_bestaat() -> None:
    """Een gate met een onbekend subcommando faalt pas in CI, en dan op de verkeerde reden."""
    gebruikt = set()
    for workflow in (WORKFLOW, SECURITY_WORKFLOW):
        for job in yaml.safe_load(workflow.read_text(encoding="utf-8"))["jobs"].values():
            for stap in job.get("steps", []):
                for regel in str(stap.get("run", "")).splitlines():
                    if "opi.tools.aanvalsproef" in regel:
                        soort = regel.split("opi.tools.aanvalsproef", 1)[1].split()[0]
                        gebruikt.add(soort)
    assert gebruikt, "geen enkele gate roept opi.tools.aanvalsproef aan"
    assert gebruikt <= {"grendel", *LEZERS}, f"onbekende subcommando's: {gebruikt - {'grendel', *LEZERS}}"
