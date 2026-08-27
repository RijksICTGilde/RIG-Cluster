"""De clustercatalogus komt uit clusters.yaml en niet meer uit een dict in de code.

De omzetting zelf is eenmalig bewezen door de geladen YAML te vergelijken met de dict zoals
die in git stond: identiek, alle vier de clusters, alle 36 sleutels. Dat is een controle die
je maar een keer kunt doen, want daarna bestaat het origineel niet meer.

Wat hier blijvend bewaakt wordt is het andere: dat het laden doet wat het belooft, dat een
kapotte catalogus een leesbare startfout geeft in plaats van een KeyError diep in een
aanroep, en dat de gemounte versie wint van de meegeleverde. Dat laatste is de hele reden
voor de verhuizing: een nieuw cluster hoort configuratie te zijn en geen codewijziging met
een nieuwe image erachteraan.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
import yaml
from opi.core import cluster_config as cc

if TYPE_CHECKING:
    from pathlib import Path


class TestWatErGeladenIs:
    def test_het_bestand_en_de_dict_zijn_hetzelfde(self) -> None:
        """De loader vormt niets om.

        Bewust GEEN Pydantic-objecten terug: 58 accessors lezen dicts, een handvol modules
        leest CLUSTER_CONFIG rechtstreeks, en drie tests zetten er met patch.dict een sleutel
        in. Het model valideert bij het laden en verandert de vorm niet.
        """
        rauw = yaml.safe_load(cc.MEEGELEVERDE_CATALOGUS.read_text(encoding="utf-8"))

        assert rauw["clusters"] == cc.CLUSTER_CONFIG

    def test_de_vier_clusters_staan_erin(self) -> None:
        assert set(cc.CLUSTER_CONFIG) == {"local", "sandboxed-local", "fundament-poc", "odcn-production"}

    @pytest.mark.parametrize(
        ("cluster", "sleutel", "waarde"),
        [
            ("odcn-production", "namespace", "rig-prd-operations"),
            ("odcn-production", "uses_capsule", True),
            ("fundament-poc", "ingress_postfix", ".fundament-poc.rijksapp.dev"),
            ("fundament-poc", "mail_relay_namespace", "rig-ron"),
            ("sandboxed-local", "supports_vpa", False),
        ],
    )
    def test_een_greep_uit_de_waarden_klopt_nog(self, cluster: str, sleutel: str, waarde: Any) -> None:
        """Een handvol waarden die pijn doen als ze verschuiven.

        Niet om de hele catalogus te herhalen (dat is de vorige test), maar om een verminkte
        handmatige bewerking te betrappen: de namespace van productie, de Capsule-vlag die
        bepaalt of ArgoCD tenant-scoped is, en de postfix waar elke URL uit volgt.
        """
        assert cc.CLUSTER_CONFIG[cluster][sleutel] == waarde


class TestEenKapotteCatalogusStoptDeStart:
    """Een leesbare fout bij het laden, en niet een KeyError halverwege een aanroep.

    Dat is geen theoretisch onderscheid. Precies die klasse van fout nam deze week de boot
    van OPI mee: get_mail_from_address beloofde ValueError en gaf KeyError op een cluster
    zonder mail-sleutels, en de aanroeper ving alleen het eerste.
    """

    @staticmethod
    def _schrijf(tmp_path: Path, catalogus: dict[str, Any]) -> Path:
        pad = tmp_path / "clusters.yaml"
        pad.write_text(yaml.safe_dump({"clusters": catalogus}), encoding="utf-8")
        return pad

    def test_een_onbekende_sleutel_wordt_geweigerd(self, tmp_path: Path, monkeypatch) -> None:
        """extra=forbid, zodat een typefout in een sleutelnaam niet stil genegeerd wordt.

        Dat een NIEUW veld daarmee ook het model raakt is de bedoeling: een nieuw CLUSTER is
        configuratie en kost geen code, een nieuw VELD is nieuw gedrag en kost dus wel code.
        """
        kapot = dict(cc.CLUSTER_CONFIG["fundament-poc"])
        kapot["ingres_postfix"] = ".typefout"
        pad = self._schrijf(tmp_path, {"fundament-poc": kapot})
        monkeypatch.setattr(cc, "GEMOUNTE_CATALOGUS", pad)

        with pytest.raises(RuntimeError, match="fundament-poc"):
            cc._laad_catalogus()

    def test_een_ontbrekende_verplichte_sleutel_wordt_geweigerd(self, tmp_path: Path, monkeypatch) -> None:
        kapot = dict(cc.CLUSTER_CONFIG["fundament-poc"])
        del kapot["namespace"]
        pad = self._schrijf(tmp_path, {"fundament-poc": kapot})
        monkeypatch.setattr(cc, "GEMOUNTE_CATALOGUS", pad)

        with pytest.raises(RuntimeError, match="fundament-poc"):
            cc._laad_catalogus()

    def test_de_mailsleutels_mogen_ontbreken(self, tmp_path: Path, monkeypatch) -> None:
        """Ze staan op alle vier de clusters, en toch zijn ze optioneel.

        has_mail_relay bestaat juist omdat een cluster zonder relay een normale toestand is,
        en die functie leest de AFWEZIGHEID van mail_relay_host. Zou het model ze verplicht
        maken, dan was die functie dood en zou een cluster zonder relay niet meer kunnen
        bestaan.
        """
        zonder = {k: v for k, v in cc.CLUSTER_CONFIG["fundament-poc"].items() if not k.startswith("mail_")}
        pad = self._schrijf(tmp_path, {"fundament-poc": zonder})
        monkeypatch.setattr(cc, "GEMOUNTE_CATALOGUS", pad)

        catalogus = cc._laad_catalogus()

        assert "mail_relay_host" not in catalogus["fundament-poc"]

    def test_een_bestand_zonder_clusters_wordt_geweigerd(self, tmp_path: Path, monkeypatch) -> None:
        pad = tmp_path / "clusters.yaml"
        pad.write_text("iets_anders: {}\n", encoding="utf-8")
        monkeypatch.setattr(cc, "GEMOUNTE_CATALOGUS", pad)

        with pytest.raises(RuntimeError, match="clusters"):
            cc._laad_catalogus()


class TestDeGemounteVersieWint:
    """Zonder dit is de verhuizing zinloos.

    De ConfigMap operations-manager-config wordt als volume op /etc/config gezet, zonder
    subPath, dus elke sleutel wordt daar een bestand. Een sleutel clusters.yaml toevoegen
    laat /etc/config/clusters.yaml vanzelf verschijnen. Pas als die wint boven het
    meegeleverde bestand kost een nieuw cluster geen nieuwe image meer.
    """

    def test_een_gemounte_catalogus_vervangt_de_meegeleverde(self, tmp_path: Path, monkeypatch) -> None:
        eigen = dict(cc.CLUSTER_CONFIG["fundament-poc"])
        eigen["ingress_postfix"] = ".uit-de-configmap"
        pad = tmp_path / "clusters.yaml"
        pad.write_text(yaml.safe_dump({"clusters": {"fundament-poc": eigen}}), encoding="utf-8")
        monkeypatch.setattr(cc, "GEMOUNTE_CATALOGUS", pad)

        catalogus = cc._laad_catalogus()

        assert list(catalogus) == ["fundament-poc"]
        assert catalogus["fundament-poc"]["ingress_postfix"] == ".uit-de-configmap"

    def test_zonder_mount_valt_hij_terug_op_het_meegeleverde_bestand(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(cc, "GEMOUNTE_CATALOGUS", tmp_path / "bestaat-niet.yaml")

        catalogus = cc._laad_catalogus()

        assert set(catalogus) == set(cc.CLUSTER_CONFIG)
