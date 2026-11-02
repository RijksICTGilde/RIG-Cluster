"""Elk Secret onder **/overlays/odcn*/ hoort SOPS-versleuteld in git te staan.

De secret-scan kijkt naar sleutelVORMEN, dus korte wachtwoorden ontsnappen.
Deze toets kijkt naar de documentsoort en draait in CI.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from secret_scan import tracked_files  # noqa: E402

#: Paden die een plat Secret MOGEN dragen, met de reden. Een pad dat hier veroudert (niet meer
#: onder een productie-overlay), laat de toets falen.
TOEGESTANE_PLATTE_SECRETS: dict[str, str] = {}

#: De velden waarin een Secret zijn waarden draagt.
_GEHEIMVELDEN = ("stringData", "data")


def _is_productie_overlay(relatief: Path) -> bool:
    """Een segment `overlays` gevolgd door een overlay die met `odcn` begint."""
    delen = relatief.parts
    return any(deel == "overlays" and delen[i + 1].startswith("odcn") for i, deel in enumerate(delen[:-1]))


def productie_overlay_bestanden(root: Path, paden: list[Path]) -> list[Path]:
    """De YAML-bestanden onder een odcn-overlay, relatief aan de root."""
    return sorted(
        pad.relative_to(root)
        for pad in paden
        if pad.suffix in (".yaml", ".yml") and _is_productie_overlay(pad.relative_to(root))
    )


def _overtredingen_in_bestand(pad: Path, relatief: Path) -> list[str]:
    """De Secret-documenten in een bestand die hun velden onversleuteld dragen.

    Een bestand dat niet parset telt ook, anders slaat de toets stil bestanden over.
    """
    try:
        documenten = list(yaml.safe_load_all(pad.read_text()))
    except yaml.YAMLError as fout:
        return [f"{relatief}: niet te lezen ({type(fout).__name__})"]

    overtredingen = []
    for document in documenten:
        if not isinstance(document, dict) or document.get("kind") != "Secret" or "sops" in document:
            continue
        velden = [veld for veld in _GEHEIMVELDEN if document.get(veld)]
        if velden:
            naam = (document.get("metadata") or {}).get("name", "<zonder naam>")
            overtredingen.append(f"{relatief}: Secret '{naam}' draagt {', '.join(velden)} onversleuteld")
    return overtredingen


def platte_secret_overtredingen(root: Path, paden: list[Path], toegestaan: dict[str, str]) -> list[str]:
    """Elk Secret onder een productie-overlay dat niet SOPS-versleuteld is.

    De melding noemt het pad, de naam en de veldnamen, nooit de waarden: een CI-log is geen
    plek voor de inhoud van een secret.
    """
    bestanden = productie_overlay_bestanden(root, paden)
    overtredingen = [
        melding
        for relatief in bestanden
        if str(relatief) not in toegestaan
        for melding in _overtredingen_in_bestand(root / relatief, relatief)
    ]
    verouderd = sorted(set(toegestaan) - {str(relatief) for relatief in bestanden})
    return overtredingen + [
        f"{pad}: staat op TOEGESTANE_PLATTE_SECRETS maar ligt niet meer onder een productie-overlay"
        for pad in verouderd
    ]


def _schrijf(root: Path, relatief: str, inhoud: str) -> Path:
    pad = root / relatief
    pad.parent.mkdir(parents=True, exist_ok=True)
    pad.write_text(inhoud)
    return pad


_PLAT_MET_STRINGDATA = """apiVersion: v1
kind: Secret
metadata:
  name: keycloak-db-credentials
stringData:
  password: nep-wachtwoord
"""

_PLAT_MET_DATA = """apiVersion: v1
kind: Secret
metadata:
  name: minio-admin
data:
  MINIO_ROOT_PASSWORD: bmVw
"""

# Ingesprongen met dedent eromheen: een `sops:` op kolom 0 laat de secret-scan dit
# python-bestand als een sops-document lezen, en dan meldt hij elke waarde erboven.
_VERSLEUTELD = textwrap.dedent("""\
    apiVersion: v1
    kind: Secret
    metadata:
      name: transip
    stringData:
      token: ENC[AES256_GCM,data:Zm9v,type:str]
    sops:
      age:
        - recipient: age1nep
      version: 3.13.1
    """)


class TestDeRepo:
    # tracked_files() volgt git, dus een los bestand in de werkmap valt erbuiten. Dat is de
    # goede grens: wat niet gecommit is, bereikt productie niet.
    def test_geen_plat_secret_in_een_productie_overlay(self) -> None:
        overtredingen = platte_secret_overtredingen(_REPO_ROOT, tracked_files(_REPO_ROOT), TOEGESTANE_PLATTE_SECRETS)
        assert not overtredingen, "Zet deze Secrets als SOPS-bestand in git (features/sops-sleutel-roteren.md):\n" + (
            "\n".join(overtredingen)
        )

    def test_de_overlays_worden_echt_gevonden(self) -> None:
        """Zonder dit is een filter dat niets meer selecteert niet te onderscheiden van een schone repo."""
        assert len(productie_overlay_bestanden(_REPO_ROOT, tracked_files(_REPO_ROOT))) > 50


class TestDeToetsBijt:
    def test_een_plat_secret_wordt_gemeld(self, tmp_path: Path) -> None:
        paden = [
            _schrijf(tmp_path, "config/overlays/odcn/db.yaml", _PLAT_MET_STRINGDATA),
            _schrijf(tmp_path, "config/overlays/odcn-production/minio.yaml", _PLAT_MET_DATA),
            _schrijf(tmp_path, "config/overlays/odcn/transip.sops.yaml", _VERSLEUTELD),
        ]
        overtredingen = platte_secret_overtredingen(tmp_path, paden, {})
        assert overtredingen == [
            "config/overlays/odcn/db.yaml: Secret 'keycloak-db-credentials' draagt stringData onversleuteld",
            "config/overlays/odcn-production/minio.yaml: Secret 'minio-admin' draagt data onversleuteld",
        ]

    def test_de_melding_noemt_de_waarde_niet(self, tmp_path: Path) -> None:
        pad = _schrijf(tmp_path, "config/overlays/odcn/db.yaml", _PLAT_MET_STRINGDATA)
        assert "nep-wachtwoord" not in "\n".join(platte_secret_overtredingen(tmp_path, [pad], {}))

    def test_een_sandbox_overlay_valt_erbuiten(self, tmp_path: Path) -> None:
        """Daar staan bewust Kind-defaults in; die zijn een ander gesprek."""
        pad = _schrijf(tmp_path, "config/overlays/sandboxed-local/db.yaml", _PLAT_MET_STRINGDATA)
        assert platte_secret_overtredingen(tmp_path, [pad], {}) == []

    @pytest.mark.parametrize("veld", _GEHEIMVELDEN)
    def test_een_leeg_veld_is_geen_overtreding(self, tmp_path: Path, veld: str) -> None:
        pad = _schrijf(
            tmp_path, "config/overlays/odcn/leeg.yaml", f"kind: Secret\nmetadata:\n  name: leeg\n{veld}: {{}}\n"
        )
        assert platte_secret_overtredingen(tmp_path, [pad], {}) == []

    def test_een_bestand_dat_niet_parset_is_een_overtreding(self, tmp_path: Path) -> None:
        pad = _schrijf(tmp_path, "config/overlays/odcn/kapot.yaml", "kind: Secret\nstringData: {onafgesloten\n")
        overtredingen = platte_secret_overtredingen(tmp_path, [pad], {})
        assert len(overtredingen) == 1
        assert overtredingen[0].startswith("config/overlays/odcn/kapot.yaml: niet te lezen (")


class TestDeUitzonderingslijst:
    def test_een_pad_op_de_lijst_wordt_niet_gemeld(self, tmp_path: Path) -> None:
        pad = _schrijf(tmp_path, "config/overlays/odcn/db.yaml", _PLAT_MET_STRINGDATA)
        toegestaan = {"config/overlays/odcn/db.yaml": "wacht op de sleutelronde"}
        assert platte_secret_overtredingen(tmp_path, [pad], toegestaan) == []

    def test_een_verouderd_pad_op_de_lijst_is_een_overtreding(self, tmp_path: Path) -> None:
        toegestaan = {"config/overlays/odcn/weg.yaml": "verhuisd"}
        assert platte_secret_overtredingen(tmp_path, [], toegestaan) == [
            "config/overlays/odcn/weg.yaml: staat op TOEGESTANE_PLATTE_SECRETS maar ligt niet meer "
            "onder een productie-overlay"
        ]

    def test_de_lijst_van_vandaag_is_leeg(self) -> None:
        """Een uitzondering toevoegen hoort deze toets te breken, zodat de reden langs review komt."""
        assert TOEGESTANE_PLATTE_SECRETS == {}
