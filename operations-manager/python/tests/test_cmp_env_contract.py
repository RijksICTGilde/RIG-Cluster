"""``.cmp-env`` is configuratie, geen script, en de plugin leest het als gegevens.

Een helmfile-deployment geeft de CMP configuratie door via een bestand ``.cmp-env`` in de
deploymentmap. Dat bestand komt uit het projectbestand van een tenant. De plugin deed
``set -a; source .cmp-env; set +a``, en daarmee voerde bash de inhoud uit als script, in
hetzelfde proces dat daarna de AGE-sleutel ophaalt en ``ARGOCD_APP_NAMESPACE``,
``SOPS_KEY_SECRET`` en ``KUSTOMIZE_FOLDERS`` leest. Een regel in ``.cmp-env`` kon dus ook
die variabelen zetten of willekeurige commando's starten.

Deze test pint vier dingen vast:

1. **Alles wat de plugin uit de omgeving leest is gereserveerd**, op de variabelen na die
   een deployment juist mag meegeven. Komt er een nieuwe variabele in het script zonder
   dat hij op de lijst gaat, dan wordt deze test rood.
2. **De parser voert niets uit.** Shell-syntaxis in een waarde blijft letterlijk.
3. **De gereserveerde lijst in het script is dezelfde als in Python.** Het script is shell
   en kan de constante niet importeren.
4. **Het schema weigert wat de plugin weigert**, zodat een fout bij het opslaan opvalt en
   niet pas bij de render. De schrijfkant zelf staat in
   ``tests/test_helmfile_env_vars_decrypt.py``, bij de wiring die daar al stond.
"""

from __future__ import annotations

import copy
import re
import subprocess
from pathlib import Path

import pytest
from opi.core.project_schema import ProjectSchemaError, validate_project_schema
from opi.generation.manifests import CMP_ENV_RESERVED_NAMES, CMP_ENV_RESERVED_PREFIXES
from opi.services.schema_migration import LATEST_SCHEMA_VERSION, migrate_to_latest
from ruamel.yaml import YAML
from tests.programma import echt_programma

_PLUGIN = Path(__file__).parent.parent.parent.parent / "bootstrap/rig-system/kustomize/configmap-sops-plugin.yaml"

#: Variabelen die een deployment juist WEL via .cmp-env mag zetten. ``ENVIRONMENT`` kiest
#: de helmfile-environment en is de reden dat het bestand bestaat.
_TENANT_INPUT = frozenset({"ENVIRONMENT"})

#: Wat de plugin deed voorheen, als meetlat voor "bestaande projecten merken niets".
_SOURCE = "set -a\nsource .cmp-env\nset +a"

_AGE_BLOCK = "-----BEGIN AGE ENCRYPTED FILE-----\nY2lwaGVydGV4dA==\n-----END AGE ENCRYPTED FILE-----\n"


def _generate_script() -> str:
    """Het bash-script uit de ``generate``-stap van de ConfigManagementPlugin."""
    configmap = YAML(typ="safe").load(_PLUGIN.read_text())
    plugin = YAML(typ="safe").load(configmap["data"]["plugin.yaml"])
    return plugin["spec"]["generate"]["args"][-1]


def _load_cmp_env_function(script: str) -> str:
    match = re.search(r"^load_cmp_env\(\) \{\n.*?^\}$", script, re.DOTALL | re.MULTILINE)
    assert match, "de parser staat niet meer waar de test hem zoekt"
    return match.group(0)


def _variables_read_from_the_environment(script: str) -> set[str]:
    """Variabelen die het script uitleest zonder ze eerst zelf te zetten."""
    used = set(re.findall(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)", script))
    assigned = set(re.findall(r"^\s*(?:export\s+|local\s+|declare\s+)?([A-Za-z_][A-Za-z0-9_]*)=", script, re.MULTILINE))
    assigned |= set(re.findall(r"\bfor\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\b", script))
    assigned |= set(re.findall(r"\bread\s+(?:-r\s+)?([A-Za-z_][A-Za-z0-9_]*)", script))
    for declaration in re.findall(r"^\s*local\s+(.*)$", script, re.MULTILINE):
        assigned |= {word.split("=")[0] for word in declaration.split()}
    return used - assigned


def _is_reserved(name: str) -> bool:
    return name in CMP_ENV_RESERVED_NAMES or name.startswith(CMP_ENV_RESERVED_PREFIXES)


def _load(loader: str, cmp_env: str, tmp_path: Path, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Laadt een ``.cmp-env`` in ``tmp_path`` met ``loader`` en toont de omgeving die dat oplevert."""
    bash = echt_programma("bash")
    (tmp_path / ".cmp-env").write_text(cmp_env)
    runner = tmp_path / "runner.sh"
    runner.write_text(f"set -e\nset -o pipefail\n{loader}\nprintenv\n")
    return subprocess.run(
        [bash, str(runner)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", **environment},
    )


def _parse(cmp_env: str, tmp_path: Path, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Draait alleen de parser uit het pluginscript."""
    loader = f"{_load_cmp_env_function(_generate_script())}\nload_cmp_env .cmp-env"
    return _load(loader, cmp_env, tmp_path, environment)


def _exported(result: subprocess.CompletedProcess[str]) -> dict[str, str]:
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def test_wat_de_plugin_uit_de_omgeving_leest_is_gereserveerd() -> None:
    """De tegenhanger van de lijst: een nieuwe variabele in het script moet mee."""
    leftovers = _variables_read_from_the_environment(_generate_script())

    assert leftovers, "de inventarisatie vindt niets meer, dus hij meet niets"
    niet_afgedekt = {name for name in leftovers - _TENANT_INPUT if not _is_reserved(name)}
    assert not niet_afgedekt, (
        f"het pluginscript leest {sorted(niet_afgedekt)} uit de omgeving: zet ze op "
        "CMP_ENV_RESERVED_PREFIXES/NAMES en in het case-patroon van de plugin, of neem ze "
        "op in _TENANT_INPUT als een deployment ze juist mag zetten"
    )


def test_het_script_reserveert_precies_dezelfde_namen_als_python() -> None:
    patroon = re.search(
        r"^\s*(ARGOCD_\*\|.*?)\)$", _load_cmp_env_function(_generate_script()), re.MULTILINE | re.DOTALL
    )
    assert patroon, "het case-patroon met gereserveerde namen staat niet meer in de parser"

    in_script = set(patroon.group(1).split("|"))
    in_python = {f"{prefix}*" for prefix in CMP_ENV_RESERVED_PREFIXES} | set(CMP_ENV_RESERVED_NAMES)

    assert in_script == in_python


def test_een_gewone_waarde_komt_letterlijk_aan(tmp_path: Path) -> None:
    """Ongequote spaties hoorden bij ``source`` nog bij het volgende woord."""
    result = _parse("MELDING=een waarde met spaties\n", tmp_path, {})

    assert result.returncode == 0, result.stderr
    assert _exported(result)["MELDING"] == "een waarde met spaties"


def test_shell_syntaxis_in_een_waarde_wordt_niet_uitgevoerd(tmp_path: Path) -> None:
    """Met ``source`` draaide dit drie commando's; nu is het de waarde."""
    waarde = "$(touch uitgevoerd); `touch uitgevoerd`; touch uitgevoerd"

    result = _parse(f"NUTTELOOS={waarde}\n", tmp_path, {})

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "uitgevoerd").exists(), "de parser heeft de waarde als script uitgevoerd"
    assert _exported(result)["NUTTELOOS"] == waarde


def test_een_gereserveerde_sleutel_stopt_de_render(tmp_path: Path) -> None:
    result = _parse("ARGOCD_APP_NAMESPACE=rig-van-de-tenant\n", tmp_path, {"ARGOCD_APP_NAMESPACE": "rig-echt"})

    assert result.returncode != 0
    assert "ARGOCD_APP_NAMESPACE" in result.stderr


def test_de_namespace_van_argocd_overleeft_het_parsen(tmp_path: Path) -> None:
    result = _parse("ENVIRONMENT=productie\n", tmp_path, {"ARGOCD_APP_NAMESPACE": "rig-echt"})

    assert result.returncode == 0, result.stderr
    geexporteerd = _exported(result)
    assert geexporteerd["ARGOCD_APP_NAMESPACE"] == "rig-echt"
    assert geexporteerd["ENVIRONMENT"] == "productie"


@pytest.mark.parametrize("regel", ["", "# een commentaarregel"])
def test_lege_regels_en_commentaar_worden_overgeslagen(regel: str, tmp_path: Path) -> None:
    result = _parse(f"{regel}\nWEL=ja\n", tmp_path, {})

    assert result.returncode == 0, result.stderr
    assert _exported(result)["WEL"] == "ja"


def test_een_regel_zonder_isgelijkteken_stopt_de_render(tmp_path: Path) -> None:
    result = _parse("ZONDER_WAARDE\n", tmp_path, {})

    assert result.returncode != 0
    assert "line 1" in result.stderr


def test_een_ongeldige_sleutel_stopt_de_render_en_noemt_de_sleutel(tmp_path: Path) -> None:
    result = _parse("1FOUT=ja\n", tmp_path, {})

    assert result.returncode != 0
    assert "1FOUT" in result.stderr


def _helmfile_project(env_vars: dict[str, str]) -> dict[str, object]:
    """Een helmfile-project in de vorm die features/helmfile-single-app-deployment.md beschrijft.

    De echte helmfile-projecten staan in de zad-projects-repo, niet hier; deze fixture
    houdt hun vorm vast: een platte ``ENVIRONMENT`` plus een versleutelde waarde.
    """
    return {
        "name": "mb-docs",
        "description": "Helmfile deployment",
        "clusters": ["odcn-production"],
        "users": [{"email": "admin@rijksoverheid.nl", "role": "admin"}],
        "helmfile": [{"name": "docs", "url": "https://git.example.org/mijn-bureau-infra.git"}],
        "deployments": [
            {
                "name": "productie",
                "cluster": "odcn-production",
                "namespace": "mb-docs",
                "helmfile": [{"reference": "docs", "env-vars": env_vars}],
            }
        ],
    }


def test_de_vorm_van_een_bestaand_helmfile_project_blijft_valide() -> None:
    project = _helmfile_project({"ENVIRONMENT": "production", "MASTER_PASSWORD": _AGE_BLOCK})
    project["schema-version"] = LATEST_SCHEMA_VERSION

    gemigreerd, _ = migrate_to_latest(copy.deepcopy(project))

    validate_project_schema(gemigreerd)


@pytest.mark.parametrize("sleutel", ["MET SPATIE", "MET=ISGELIJK", "1LEIDEND_CIJFER", "MET-STREEPJE"])
def test_het_schema_weigert_een_sleutel_die_geen_variabelenaam_is(sleutel: str) -> None:
    with pytest.raises(ProjectSchemaError):
        validate_project_schema(_helmfile_project({sleutel: "waarde"}))


def test_het_schema_weigert_een_waarde_met_een_regeleinde() -> None:
    """Twee regels in een waarde zijn twee regels in .cmp-env, en de tweede is onbewaakt."""
    with pytest.raises(ProjectSchemaError):
        validate_project_schema(_helmfile_project({"MELDING": "eerste\nARGOCD_APP_NAMESPACE=kaper"}))


@pytest.mark.parametrize(
    "paar",
    ["ENVIRONMENT=production", "MASTER_PASSWORD=Ab3-xY_9.qP:7", "PAD=/srv/data/docs", "LEEG="],
)
def test_een_gewone_regel_levert_hetzelfde_op_als_source_deed(paar: str, tmp_path: Path) -> None:
    """Bestaande helmfile-projecten renderen identiek na de parser-wijziging.

    Shell-syntaxis in een waarde wordt niet getest; die werd door ``source`` uitgevoerd,
    en blijft nu letterlijk.
    """
    sleutel = paar.split("=", 1)[0]
    (tmp_path / "parser").mkdir()
    (tmp_path / "source").mkdir()

    met_parser = _exported(_parse(f"{paar}\n", tmp_path / "parser", {}))
    met_source = _exported(_load(_SOURCE, f"{paar}\n", tmp_path / "source", {}))

    assert met_parser[sleutel] == met_source[sleutel]


def _helmfile_argv(environment: str | None, tmp_path: Path) -> list[str]:
    """De argumenten waarmee het pluginscript ``helmfile`` aanroept.

    Draait ``process_helmfile_folder`` uit de ConfigMap met stubs voor de stappen die
    een cluster nodig hebben, en vangt de argv op.
    """
    bash = echt_programma("bash")
    script = _generate_script()
    functie = re.search(r"^process_helmfile_folder\(\) \{\n.*?^\}$", script, re.DOTALL | re.MULTILINE)
    assert functie, "process_helmfile_folder staat niet meer waar de test hem zoekt"

    (tmp_path / "helmfile.yaml").write_text("releases: []\n")
    runner = tmp_path / "runner.sh"
    runner.write_text(
        "set -e\nset -o pipefail\n"
        "decrypt_helmfile_values() { :; }\n"
        "helm() { echo diff; }\n"
        'helmfile() { printf "%s\\n" "$@" > argv.txt; }\n'
        f"{functie.group(0)}\n"
        'process_helmfile_folder "." "/dev/null" "helmfile.yaml"\n'
    )
    result = subprocess.run(
        [bash, str(runner)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "ARGOCD_APP_NAMESPACE": "rig-prd-mb-docs"}
        | ({} if environment is None else {"ENVIRONMENT": environment}),
    )
    assert result.returncode == 0, result.stderr
    return (tmp_path / "argv.txt").read_text().splitlines()


def test_een_environment_uit_cmp_env_blijft_een_argument(tmp_path: Path) -> None:
    """Een waarde met spaties mag geen extra vlaggen op de helmfile-regel zetten.

    De parser maakt van .cmp-env gegevens, maar ENVIRONMENT mag een deployment juist wel
    zetten. Ging die waarde ongequote de opdrachtregel op, dan koos de tenant alsnog de
    argumenten van helmfile, bijvoorbeeld welk binary het als helm draait.
    """
    argv = _helmfile_argv("production --helm-binary ./van-de-tenant.sh", tmp_path)

    assert argv == [
        "--file",
        "helmfile.yaml",
        "-e",
        "production --helm-binary ./van-de-tenant.sh",
        "template",
        "--include-crds",
    ]


def test_een_gewone_environment_geeft_dezelfde_argumenten_als_voorheen(tmp_path: Path) -> None:
    assert _helmfile_argv("production", tmp_path) == [
        "--file",
        "helmfile.yaml",
        "-e",
        "production",
        "template",
        "--include-crds",
    ]


def test_zonder_environment_blijft_de_vlag_weg(tmp_path: Path) -> None:
    assert _helmfile_argv(None, tmp_path) == ["--file", "helmfile.yaml", "template", "--include-crds"]
