"""Templates gelden voor alle clusters, dus geen hardcoded geheimen buiten annotatie.
Alleen velden met `@secret-gen` worden gegenereerd. Dit gebeurde in chisel-auth-secret.yaml
(21-10-2026), dus deze test voorkomt het voortaan.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from secret_edit import fields_of, templates  # noqa: E402

TEMPLATES_DIR = _REPO_ROOT / "infrastructure/bootstrap/infrastructure/secrets/templates"

#: Velden die GEEN geheim dragen, met de reden. De sleutel is `<bestand>:<veld>`, zodat
#: dezelfde veldnaam in een ander bestand opnieuw langs review komt.
NIET_GEHEIM: dict[str, str] = {
    "chisel-auth-secret.yaml:username": "gebruikersnaam; het wachtwoord ernaast is gegenereerd",
    "forgejo-db-credentials-secret.yaml:username": "databaserolnaam, geen geheim",
    "keycloak-admin-secret.yaml:KEYCLOAK_ADMIN": "gebruikersnaam van het break-glass-account",
    "keycloak-admin-secret.yaml:KEYCLOAK_OTP_ADMIN_USERNAME": "gebruikersnaam van de tweede beheerder",
    "keycloak-db-credentials-secret.yaml:username": "databaserolnaam, geen geheim",
    "mail-db-credentials-secret.yaml:username": "databaserolnaam, geen geheim",
    "mail-relay-secret.yaml:MAIL_FROM_LOCAL": "het vaste afzenderadres; moet gelijk zijn op elk cluster",
    "mail-relay-secret.yaml:MAIL_DOMAIN": "het vaste afzenderdomein; moet gelijk zijn op elk cluster",
    "mail-relay-secret.yaml:MAIL_RELAY_ADMIN_USERNAME": "gebruikersnaam van het relay-beheerdersaccount",
    "minio-admin-secret.yaml:MINIO_ROOT_USER": "gebruikersnaam van het rootaccount",
    "operations-manager-db-secret.yaml:username": "databaserolnaam, geen geheim",
    "pgadmin-secret.yaml:PGADMIN_DEFAULT_EMAIL": "inlognaam van pgAdmin, geen wachtwoord",
    "platform-backup-secret.yaml:username": "databaserolnaam, geen geheim",
    "postgres-admin-secret.yaml:username": "databaserolnaam, geen geheim",
    "seaweedfs-credentials-secret.yaml:AWS_ACCESS_KEY_ID": "identiteitsnaam van het admin-sleutelpaar; "
    "de AWS_SECRET_ACCESS_KEY ernaast is gegenereerd",
}


def _vaste_velden(inhoud: str) -> list[str]:
    """De velden van een template die een vaste, niet-lege waarde dragen.

    `fields_of` leest de annotaties, maar alleen onder `stringData`, en dat is precies wat
    de generatie vult (zie templates/README.md). Een veld onder `data` kan dus nooit
    gegenereerd worden en telt altijd mee.
    """
    document = yaml.safe_load(inhoud) or {}
    waarden = document.get("stringData") or {}
    # `skip` telt mee: zo'n veld vult een mens per cluster, dus de waarde die hier staat gaat
    # net zo letterlijk mee als een veld zonder annotatie.
    niet_gegenereerd = (None, "skip")
    zonder_annotatie = [
        veld.name for veld in fields_of(inhoud) if veld.kind in niet_gegenereerd and waarden.get(veld.name)
    ]
    onder_data = [str(naam) for naam, waarde in (document.get("data") or {}).items() if waarde]
    return zonder_annotatie + onder_data


def vaste_waarden(directory: Path, toegestaan: dict[str, str]) -> list[str]:
    """Elk veld in een template dat een vaste waarde draagt zonder benoemde reden.

    Een leeg veld telt niet: dat draagt geen geheim (vault-init vult zijn sleutels pas bij
    het initialiseren). De melding noemt bestand en veldnaam, nooit de waarde.
    """
    overtredingen: list[str] = []
    gezien: set[str] = set()
    for pad in templates(directory):
        for naam in _vaste_velden(pad.read_text()):
            sleutel = f"{pad.name}:{naam}"
            gezien.add(sleutel)
            if sleutel not in toegestaan:
                overtredingen.append(f"{sleutel}: vaste waarde die de generatie niet vervangt")
    verouderd = sorted(set(toegestaan) - gezien)
    return overtredingen + [f"{sleutel}: staat op NIET_GEHEIM maar bestaat niet meer" for sleutel in verouderd]


def _schrijf(directory: Path, naam: str, inhoud: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / naam).write_text(inhoud)
    return directory / naam


_MET_ANNOTATIE = """apiVersion: v1
kind: Secret
metadata:
  name: demo
stringData:
  password: "changeMe123!" # @secret-gen:random:20
"""

_ZONDER_ANNOTATIE = """apiVersion: v1
kind: Secret
metadata:
  name: demo
stringData:
  auth: "admin:CHANGE-THIS-PASSWORD"
"""


class TestDeRepo:
    def test_geen_template_draagt_een_vaste_geheime_waarde(self) -> None:
        overtredingen = vaste_waarden(TEMPLATES_DIR, NIET_GEHEIM)
        assert not overtredingen, (
            "Zet een @secret-gen-annotatie op dit veld, of zet het met de reden op NIET_GEHEIM "
            "(infrastructure/bootstrap/infrastructure/secrets/templates/README.md):\n" + "\n".join(overtredingen)
        )

    def test_de_templates_worden_echt_gevonden(self) -> None:
        """Zonder dit is een filter dat niets meer selecteert niet te onderscheiden van een schone boom."""
        assert len(templates(TEMPLATES_DIR)) > 10


class TestDeToetsBijt:
    def test_een_waarde_zonder_annotatie_wordt_gemeld(self, tmp_path: Path) -> None:
        _schrijf(tmp_path, "chisel-auth-secret.yaml", _ZONDER_ANNOTATIE)
        assert vaste_waarden(tmp_path, {}) == [
            "chisel-auth-secret.yaml:auth: vaste waarde die de generatie niet vervangt"
        ]

    def test_de_melding_noemt_de_waarde_niet(self, tmp_path: Path) -> None:
        _schrijf(tmp_path, "demo-secret.yaml", _ZONDER_ANNOTATIE)
        assert "CHANGE-THIS-PASSWORD" not in "\n".join(vaste_waarden(tmp_path, {}))

    def test_een_geannoteerde_waarde_is_geen_overtreding(self, tmp_path: Path) -> None:
        _schrijf(tmp_path, "demo-secret.yaml", _MET_ANNOTATIE)
        assert vaste_waarden(tmp_path, {}) == []

    @pytest.mark.parametrize("veld", ["stringData", "data"])
    def test_een_leeg_veld_is_geen_overtreding(self, tmp_path: Path, veld: str) -> None:
        _schrijf(tmp_path, "demo-secret.yaml", f'kind: Secret\n{veld}:\n  root-token: ""\n')
        assert vaste_waarden(tmp_path, {}) == []

    def test_een_veld_onder_data_kan_nooit_gegenereerd_zijn(self, tmp_path: Path) -> None:
        """De generatie schrijft alleen `stringData`, dus een annotatie onder `data` doet niets."""
        _schrijf(tmp_path, "demo-secret.yaml", "kind: Secret\ndata:\n  token: bmVw # @secret-gen:random:20\n")
        assert vaste_waarden(tmp_path, {}) == ["demo-secret.yaml:token: vaste waarde die de generatie niet vervangt"]

    def test_een_skip_annotatie_telt_ook_mee(self, tmp_path: Path) -> None:
        """`skip` betekent "vul dit met de hand", dus de waarde in de template gaat letterlijk mee."""
        _schrijf(tmp_path, "demo-secret.yaml", "kind: Secret\nstringData:\n  password: geheim # @secret-gen:skip\n")
        assert vaste_waarden(tmp_path, {}) == ["demo-secret.yaml:password: vaste waarde die de generatie niet vervangt"]

    def test_kustomization_en_readme_vallen_erbuiten(self, tmp_path: Path) -> None:
        _schrijf(tmp_path, "kustomization.yaml", "resources:\n  - demo-secret.yaml\n")
        assert vaste_waarden(tmp_path, {}) == []


class TestDeUitzonderingslijst:
    def test_een_veld_op_de_lijst_wordt_niet_gemeld(self, tmp_path: Path) -> None:
        _schrijf(tmp_path, "chisel-auth-secret.yaml", _ZONDER_ANNOTATIE)
        assert vaste_waarden(tmp_path, {"chisel-auth-secret.yaml:auth": "nep"}) == []

    def test_een_verouderd_veld_op_de_lijst_is_een_overtreding(self, tmp_path: Path) -> None:
        assert vaste_waarden(tmp_path, {"weg-secret.yaml:username": "verhuisd"}) == [
            "weg-secret.yaml:username: staat op NIET_GEHEIM maar bestaat niet meer"
        ]

    def test_de_lijst_draagt_per_veld_een_reden(self) -> None:
        assert all(reden.strip() for reden in NIET_GEHEIM.values())
