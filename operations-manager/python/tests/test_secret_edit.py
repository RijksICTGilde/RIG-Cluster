"""Toetsen voor scripts/secret_edit.py: één veld vervangen zonder de rest te roteren.

Vier eigenschappen dragen dit script, en "hij draait" toetst er geen van:

* de naamgeving is sluitend. `decrypt` gevolgd door `encrypt` moet op het bestand uitkomen waar
  het begon. De oude Taskfile-ronde deed dat niet en liet
  `keycloak-admin-secret.yaml.yaml.sops.yaml` achter, een wees die door geen enkele
  `decrypt-sops.yaml` werd gerenderd. Die ronde wordt hier op de Taskfile zelf nagelopen;
* de velden die je NIET aanraakt komen byte-voor-byte terug. Dat is het hele bestaansrecht:
  de generatie-task kan dit niet en moest het secret weggooien, wat Keycloak, MinIO en
  PostgreSQL tegelijk roteert;
* de `@secret-gen`-annotaties overleven de ronde. Ze zijn de enige plek waar staat hoe een veld
  opnieuw gemaakt moet worden, en de wees hierboven is ze bij een handbewerking kwijtgeraakt;
* de sleutel komt uit het bestand en niet uit een vaste `security/key.txt`, want anders wordt
  een sandbox-secret op de productiesleutel teruggeschreven zonder melding.

Er wordt op echte ciphertext getoetst: `sops` en `age-keygen` zijn nodig, en zonder die
binaries slaan de betreffende toetsen over.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import secret_edit as tool  # type: ignore[reportMissingImports]  # noqa: E402
from key_rotation import public_key_of, read_key, sops_recipients  # type: ignore[reportMissingImports]  # noqa: E402

REPO = Path(__file__).resolve().parents[3]

needs_sops = pytest.mark.skipif(shutil.which("sops") is None, reason="vraagt de sops-binary")
needs_age = pytest.mark.skipif(shutil.which("age-keygen") is None, reason="vraagt age-keygen")

TEMPLATE = """apiVersion: v1
kind: Secret
metadata:
  name: demo-credentials
  namespace: placeholder
type: Opaque
stringData:
  USERNAME: admin
  PASSWORD: "changeMe123!" # @secret-gen:random:16
  TOKEN: "changeMe123!" # @secret-gen:random:32
  LEAVE_ALONE: "vast" # @secret-gen:skip
"""


@pytest.fixture
def key(tmp_path: Path) -> Path:
    """Een echte AGE-sleutel, want de ronde moet op echte ciphertext lopen."""
    path = tmp_path / "test-key.txt"
    subprocess.run(["age-keygen", "-o", str(path)], capture_output=True, check=True)
    return path


# --------------------------------------------------------------------------- de naamgeving


def test_de_naam_van_een_versleuteld_bestand_is_de_template_plus_sops_yaml() -> None:
    """`<naam>.yaml` wordt `<naam>.yaml.sops.yaml`: dat is waar elke decrypt-sops.yaml naar wijst."""
    assert tool.encrypted_name("keycloak-admin-secret.yaml") == "keycloak-admin-secret.yaml.sops.yaml"


def test_de_leesbare_naam_verdubbelt_de_extensie_niet() -> None:
    """De bug die de wees maakte: `.sops.yaml` vervangen door `.yaml` op een naam die al op
    `.yaml` eindigde gaf `keycloak-admin-secret.yaml.yaml`, en dat werd na versleutelen een
    tweede bestand naast het origineel."""
    assert tool.plain_name("keycloak-admin-secret.yaml.sops.yaml") == "keycloak-admin-secret.yaml"
    assert tool.plain_name("los.sops.yaml") == "los.yaml"


def test_ontsleutelen_en_weer_versleutelen_komt_uit_waar_het_begon() -> None:
    """De ronde is sluitend, en dat is precies wat hij niet was."""
    start = "keycloak-admin-secret.yaml.sops.yaml"
    assert tool.encrypted_name(tool.plain_name(start)) == start


def test_de_taskfile_ronde_is_net_zo_sluitend() -> None:
    """Dezelfde eigenschap op de Taskfile zelf, want `task decrypt-secret` is waar de wees ontstond.

    De twee sed-uitdrukkingen worden uit de Taskfile gelezen en op elkaar losgelaten. Een
    regressie hier is niet zichtbaar in de Python: het zijn twee losse shell-regels.
    """
    taskfile = (REPO / "Taskfile.yaml").read_text()
    decrypt = re.search(r"^ *OUTPUT_FILE=\$\(echo \"\{\{\.FILE\}\}\" \| (sed .*)\)$", taskfile, re.MULTILINE)
    assert decrypt, "de sed van decrypt-secret is niet gevonden"

    start = "keycloak-admin-secret.yaml.sops.yaml"
    plain = subprocess.run(  # noqa: S602
        f'echo "{start}" | {decrypt.group(1)}', shell=True, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert plain == "keycloak-admin-secret.yaml", f"decrypt-secret maakt er {plain} van"

    encrypted = subprocess.run(  # noqa: S602
        f"""echo "{plain}" | sed 's/\\.yaml$/.yaml.sops.yaml/'""",
        shell=True,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert encrypted == start, f"de ronde eindigt op {encrypted} in plaats van {start}"


# --------------------------------------------------------------------------- de velden


def test_de_annotaties_worden_van_de_regel_zelf_gelezen() -> None:
    """Type en lengte komen uit `@secret-gen`, net als in `_generate-secrets-shared`."""
    fields = {field.name: field for field in tool.fields_of(TEMPLATE)}

    assert fields["PASSWORD"].kind == "random"
    assert fields["PASSWORD"].length == 16
    assert fields["TOKEN"].length == 32
    assert fields["LEAVE_ALONE"].kind == "skip"


def test_een_veld_zonder_annotatie_blijft_in_de_lijst_staan() -> None:
    """Het weglaten zou het onzichtbaar maken, terwijl je het met de hand moet kunnen zetten."""
    fields = {field.name: field for field in tool.fields_of(TEMPLATE)}

    assert fields["USERNAME"].kind is None
    assert not fields["USERNAME"].generatable


def test_alleen_random_en_bcrypt_zijn_te_genereren() -> None:
    """`skip` en een veld zonder annotatie krijgen geen genereer-keuze aangeboden."""
    generatable = {field.name for field in tool.fields_of(TEMPLATE) if field.generatable}

    assert generatable == {"PASSWORD", "TOKEN"}


def test_de_echte_templates_worden_gelezen() -> None:
    """Op de template in de repo, zodat een wijziging in zijn vorm hier opvalt."""
    template = REPO / "infrastructure/bootstrap/infrastructure/secrets/templates/keycloak-admin-secret.yaml"
    fields = {field.name: field for field in tool.fields_of(template.read_text())}

    assert fields["KEYCLOAK_ADMIN_PASSWORD"].kind == "random"
    assert fields["KEYCLOAK_ADMIN_PASSWORD"].length == 16


# --------------------------------------------------------------------------- de generatie


@pytest.mark.parametrize("length", [16, 20, 24, 32, 64])
def test_een_gegenereerde_waarde_heeft_de_gevraagde_lengte(length: int) -> None:
    """De Taskfile knipt op de lengte uit de annotatie; hetzelfde veld mag hier niet anders worden."""
    assert len(tool.generate("random", length)) == length


def test_een_gegenereerde_waarde_draagt_geen_tekens_die_de_taskfile_wegknipt() -> None:
    """`tr -d "=+/"`: die drie horen er niet in, want een `/` in een wachtwoord breekt genoeg URLs."""
    value = tool.generate("random", 64)

    assert not set(value) & set("=+/")


def test_twee_waarden_achter_elkaar_zijn_niet_gelijk() -> None:
    assert tool.generate("random", 32) != tool.generate("random", 32)


def test_een_onbekend_type_wordt_geweigerd() -> None:
    with pytest.raises(tool.EditFailed):
        tool.generate("rot13", 16)


@pytest.mark.skipif(shutil.which("htpasswd") is None, reason="vraagt htpasswd")
def test_bcrypt_levert_de_vorm_die_de_taskfile_ook_maakt() -> None:
    """`$2y$`, niet `$2b$`: de Taskfile zet dat om en sommige lezers kennen alleen de eerste."""
    assert tool.generate("bcrypt", 16).startswith("$2y$10$")


# --------------------------------------------------------------------------- de ronde


@needs_sops
@needs_age
def test_de_waarden_komen_terug_zoals_ze_erin_gingen(tmp_path: Path, key: Path) -> None:
    public = public_key_of(read_key(key))
    values = {"USERNAME": "admin", "PASSWORD": "een", "TOKEN": "twee", "LEAVE_ALONE": "vast"}
    destination = tmp_path / "demo-secret.yaml.sops.yaml"

    tool.encrypt(tool.apply(TEMPLATE, values, "rig-system"), destination, public)

    assert tool.values_of(tool.decrypt(destination, read_key(key))) == values


@needs_sops
@needs_age
def test_een_veld_vervangen_laat_de_andere_velden_ongemoeid(tmp_path: Path, key: Path) -> None:
    """Het bestaansrecht van dit script. De generatie-task kan dit niet."""
    public = public_key_of(read_key(key))
    destination = tmp_path / "demo-secret.yaml.sops.yaml"
    before = {"USERNAME": "admin", "PASSWORD": "oud", "TOKEN": "blijft", "LEAVE_ALONE": "vast"}
    tool.encrypt(tool.apply(TEMPLATE, before, "rig-system"), destination, public)

    current = tool.values_of(tool.decrypt(destination, read_key(key)))
    tool.encrypt(tool.apply(TEMPLATE, {**current, "PASSWORD": "nieuw"}, "rig-system"), destination, public)

    after = tool.values_of(tool.decrypt(destination, read_key(key)))
    assert [name for name in after if after[name] != before[name]] == ["PASSWORD"]
    assert after["PASSWORD"] == "nieuw"


@needs_sops
@needs_age
def test_sops_verschuift_de_annotaties_en_daarom_is_de_template_de_bron(tmp_path: Path, key: Path) -> None:
    """SOPS bewaart comments als losse knopen en zet ze één veld verderop terug.

    Dit is geen fout in `apply()`: voor en na `apply()` staat elke annotatie nog bij zijn eigen
    veld, en pas de SOPS-ronde schuift ze op. Het gevolg is dat een ontsleuteld secret liegt
    over zijn eigen velden, en daarom leest `edit-secret.py` de annotaties uit de TEMPLATE.
    Deze toets legt de verschuiving vast: gaat SOPS zich ooit anders gedragen, dan valt dat hier
    op en niet in een secret dat met de verkeerde lengte is gegenereerd.
    """
    public = public_key_of(read_key(key))
    destination = tmp_path / "demo-secret.yaml.sops.yaml"
    values = {field.name: "x" for field in tool.fields_of(TEMPLATE)}

    plaintext = tool.apply(TEMPLATE, values, "rig-system")
    assert [(f.name, f.kind) for f in tool.fields_of(plaintext)] == [
        (f.name, f.kind) for f in tool.fields_of(TEMPLATE)
    ], "apply() zelf mag niets verschuiven"

    tool.encrypt(plaintext, destination, public)
    after = {field.name: field.kind for field in tool.fields_of(tool.decrypt(destination, read_key(key)))}

    assert after["USERNAME"] == "random", "SOPS gedraagt zich anders dan vastgelegd: de annotaties schuiven niet meer"
    assert after["PASSWORD"] == "random"


def test_de_template_is_de_bron_van_de_annotaties() -> None:
    """De template gaat nooit door SOPS, dus daar staat elke annotatie nog bij zijn eigen veld."""
    fields = {field.name: field for field in tool.fields_of(TEMPLATE)}

    assert fields["USERNAME"].kind is None
    assert fields["PASSWORD"].length == 16
    assert fields["TOKEN"].length == 32


def test_een_veld_dat_niet_gekozen_is_gaat_uit_het_secret() -> None:
    """`values` is de VOLLEDIGE inhoud van stringData, geen aanvulling op de template.

    Het odcn-keycloak-secret kent twee van de zes velden van zijn template, want de andere vier
    zijn later aan de template toegevoegd en nooit naar dat secret doorgevoerd. Bij een ronde die
    alleen het wachtwoord roteert blijven die vier dus ongekozen, en ze moeten dan wegblijven.
    """
    result = tool.apply(TEMPLATE, {"USERNAME": "admin", "PASSWORD": "geheim"}, "rig-system")
    fields = {field.name for field in tool.fields_of(result)}

    assert fields == {"USERNAME", "PASSWORD"}
    assert "TOKEN" not in result
    assert "LEAVE_ALONE" not in result


def test_de_placeholder_van_de_template_belandt_nooit_in_een_secret() -> None:
    """De scherpe kant van de vorige toets: een ongekozen veld hield eerst de template-waarde.

    Dat is geen cosmetisch verschil maar een secret met `changeMe123!` erin, uitgerold door
    ArgoCD. Toetst op de placeholder zelf, zodat het ook opvalt als het weglaten anders wordt
    opgelost dan met een delete.
    """
    result = tool.apply(TEMPLATE, {"PASSWORD": "geheim"}, "rig-system")

    assert "changeMe123!" not in result


def test_alle_velden_kiezen_levert_alle_velden_op() -> None:
    """De andere kant op: weglaten mag geen veld verliezen dat wel gekozen is."""
    values = {field.name: "x" for field in tool.fields_of(TEMPLATE)}

    fields = {field.name for field in tool.fields_of(tool.apply(TEMPLATE, values, "rig-system"))}

    assert fields == set(values)


@needs_sops
@needs_age
def test_de_namespace_van_het_cluster_komt_in_het_secret(tmp_path: Path, key: Path) -> None:
    public = public_key_of(read_key(key))
    destination = tmp_path / "demo-secret.yaml.sops.yaml"

    tool.encrypt(tool.apply(TEMPLATE, {"USERNAME": "admin"}, "rig-prd-operations"), destination, public)

    assert "rig-prd-operations" in tool.decrypt(destination, read_key(key))


@needs_sops
@needs_age
def test_het_bestand_staat_op_de_recipient_waarvoor_het_versleuteld_is(tmp_path: Path, key: Path) -> None:
    public = public_key_of(read_key(key))
    destination = tmp_path / "demo-secret.yaml.sops.yaml"

    tool.encrypt(tool.apply(TEMPLATE, {"USERNAME": "admin"}, "rig-system"), destination, public)

    assert sops_recipients(destination) == [public]


@needs_sops
@needs_age
def test_een_verkeerde_sleutel_opent_het_bestand_niet(tmp_path: Path, key: Path) -> None:
    """Weigeren en niet stil een leeg secret teruggeven, want dat zou elk veld wissen."""
    public = public_key_of(read_key(key))
    destination = tmp_path / "demo-secret.yaml.sops.yaml"
    tool.encrypt(tool.apply(TEMPLATE, {"USERNAME": "admin"}, "rig-system"), destination, public)

    other = tmp_path / "ander.txt"
    subprocess.run(["age-keygen", "-o", str(other)], capture_output=True, check=True)

    with pytest.raises(tool.EditFailed):
        tool.decrypt(destination, read_key(other))


# --------------------------------------------------------------------------- de sleutelkeuze


@needs_age
def test_de_sleutel_wordt_bij_de_recipient_gezocht(tmp_path: Path, key: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """De guard tegen een sandbox-secret dat op de productiesleutel wordt teruggeschreven."""
    security = tmp_path / "security"
    security.mkdir()
    shutil.copy(key, security / "sandbox-key.txt")
    other = security / "key.txt"
    subprocess.run(["age-keygen", "-o", str(other)], capture_output=True, check=True)
    monkeypatch.setattr(tool, "SECURITY_DIR", security)

    assert tool.key_for(public_key_of(read_key(key))).name == "sandbox-key.txt"
    assert tool.key_for(public_key_of(read_key(other))).name == "key.txt"


@needs_age
def test_een_onbekende_recipient_wordt_geweigerd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    security = tmp_path / "security"
    security.mkdir()
    subprocess.run(["age-keygen", "-o", str(security / "key.txt")], capture_output=True, check=True)
    monkeypatch.setattr(tool, "SECURITY_DIR", security)

    with pytest.raises(tool.EditFailed, match="geen sleutel"):
        tool.key_for("age1onbekend")


@needs_age
def test_een_bestand_zonder_age_sleutel_in_security_laat_de_zoektocht_doorlopen(
    tmp_path: Path, key: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`security/` houdt ook tokens (pat_current.txt); die mogen de zoektocht niet afbreken."""
    security = tmp_path / "security"
    security.mkdir()
    (security / "pat_current.txt").write_text("ghp_geen_age_sleutel\n")
    shutil.copy(key, security / "key.txt")
    monkeypatch.setattr(tool, "SECURITY_DIR", security)

    assert tool.key_for(public_key_of(read_key(key))).name == "key.txt"


# --------------------------------------------------------------------------- de vindplaatsen


def test_de_clusters_komen_uit_de_env_taskfile_bestanden() -> None:
    """En `-current` telt niet mee: dat is een kopie van een van de andere."""
    names = {cluster.name for cluster in tool.clusters()}

    assert "odcn-production" in names
    assert "sandboxed-local" in names
    assert "current" not in names


def test_een_cluster_kent_zijn_doelmap_per_boom() -> None:
    """odcn-production schrijft infrastructuur naar `odcn/` en bootstrap naar `odcn-production/`."""
    odcn = next(cluster for cluster in tool.clusters() if cluster.name == "odcn-production")

    assert odcn.namespace == "rig-prd-operations"
    assert odcn.folders["infrastructure"] == "odcn"
    assert odcn.folders["bootstrap"] == "odcn-production"


def test_alleen_templates_die_een_secret_zijn_worden_aangeboden() -> None:
    """`kustomization.yaml` en `README.md` staan in dezelfde map; dezelfde filter als de Taskfile."""
    names = {path.name for path in tool.templates(REPO / "infrastructure/bootstrap/infrastructure/secrets/templates")}

    assert "keycloak-admin-secret.yaml" in names
    assert "kustomization.yaml" not in names
    assert all(name.endswith(("-secret.yaml", ".secret.yaml")) for name in names)
