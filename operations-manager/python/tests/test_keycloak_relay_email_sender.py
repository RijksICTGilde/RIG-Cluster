"""De uitrol van de eigen emailSender-provider van Keycloak.

Deze toetsen kijken naar MANIFESTEN en naar de Java-bron ernaast, en niet naar Python.
Dat is met opzet: alles wat hier fout kan gaan, gaat STIL fout op een draaiend cluster.

Twee gemeten eigenschappen dragen dit bestand (``docs/rc158-emailsender-spi-meting.md``):

1. ``--spi-emailSender-provider=`` (camelCase) wordt door Keycloak STIL genegeerd. Er komt
   geen waarschuwing, de pod start gewoon door, en de STANDAARDprovider verstuurt - die de
   ``smtpServer`` van de realm leest, dus precies de weg die deze hele feature dichtzet.
2. Een provider-id dat NIET bestaat laat Keycloak weigeren te starten. Dat is de canarie:
   een pod die opkomt heeft zijn provider aantoonbaar gevonden. Maar die canarie zingt pas
   bij een rollout, en dan ligt het inloggen plat. De toetsen hieronder halen dat naar
   voren: als de vlag, het provider-id in de Java-bron en de bestandsnaam van de jar uit de
   pas lopen, is dat hier rood in plaats van daar.
"""

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]

KEYCLOAK_BASE = REPO_ROOT / "infrastructure/bootstrap/infrastructure/keycloak/controller/base"
KEYCLOAK_OVERLAYS = REPO_ROOT / "infrastructure/bootstrap/infrastructure/keycloak/controller/overlays"
PROVIDER_SRC = REPO_ROOT / "keycloak-migration/relay-email-sender/src/main/java/nl/minbzk/rig/keycloak/email"

#: De vorm die Keycloak WEL leest. Zie ``RelayEmailSenderProviderFactoryTest`` aan de
#: Java-kant, die hem uit ``EmailSenderSpi.getName()`` afleidt in plaats van hem te typen.
VLAG_VOORVOEGSEL = "--spi-email-sender-provider="

#: De vorm die STIL wordt genegeerd. Mag nergens in een manifest staan.
CAMELCASE_VLAG = "--spi-emailSender-provider"


def _deployment() -> dict:
    return yaml.safe_load((KEYCLOAK_BASE / "deployment.yaml").read_text())


def _keycloak_container(deployment: dict) -> dict:
    containers = deployment["spec"]["template"]["spec"]["containers"]
    return next(c for c in containers if c["name"] == "keycloak")


def _java_constante(bestand: str, naam: str) -> str:
    """Een ``public static final String`` uit de Java-bron, zonder Java te draaien."""
    bron = (PROVIDER_SRC / bestand).read_text()
    match = re.search(rf'String {naam} = "([^"]+)";', bron)
    assert match is not None, f"constante {naam} niet gevonden in {bestand}"
    return match.group(1)


class TestDeVlag:
    """De vlag die de eigen verzender aanwijst, in de vorm die Keycloak leest."""

    def test_de_vlag_staat_in_de_args(self) -> None:
        args = _keycloak_container(_deployment())["args"]
        vlaggen = [a for a in args if a.startswith(VLAG_VOORVOEGSEL)]
        assert len(vlaggen) == 1, (
            "zonder deze vlag verstuurt de standaardprovider, en die leest de smtpServer van de realm"
        )

    def test_het_id_in_de_vlag_is_het_id_dat_de_jar_registreert(self) -> None:
        """De canarie naar voren gehaald.

        Lopen deze twee uit elkaar, dan WEIGERT Keycloak te starten - gemeten. Dat is goed
        gedrag en een slechte plek om erachter te komen: het inloggen van het hele platform
        ligt er dan bij tot iemand het terugdraait.
        """
        args = _keycloak_container(_deployment())["args"]
        vlag = next(a for a in args if a.startswith(VLAG_VOORVOEGSEL))
        id_in_de_vlag = vlag[len(VLAG_VOORVOEGSEL) :]

        assert id_in_de_vlag == _java_constante("RelayMailConfig.java", "PROVIDER_ID")

    @pytest.mark.parametrize(
        "pad",
        sorted(
            [KEYCLOAK_BASE / "deployment.yaml"]
            + [p for p in KEYCLOAK_OVERLAYS.rglob("*.yaml")]
        ),
        ids=lambda p: str(p.relative_to(REPO_ROOT)),
    )
    def test_de_camelcase_vorm_staat_nergens(self, pad: Path) -> None:
        """De val: camelCase wordt stil genegeerd en dan verstuurt de standaardprovider.

        Er is geen enkel signaal op het cluster, dus dit is de enige plek waar het gezien
        kan worden.

        COMMENTAAR TELT NIET MEE. De vorm hoort juist wel in een commentaar te staan - dat
        is waar de val wordt uitgelegd - en een toets die daarop afgaat zou het opschrijven
        van de val verbieden.
        """
        zonder_commentaar = "\n".join(
            regel for regel in pad.read_text().splitlines() if not regel.lstrip().startswith("#")
        )
        assert CAMELCASE_VLAG not in zonder_commentaar


class TestDeJarInDePod:
    """De jar komt uit een ConfigMap, en overal moet dezelfde bestandsnaam staan."""

    def _jar_uit_de_generator(self) -> str:
        kustomization = yaml.safe_load((KEYCLOAK_BASE / "kustomization.yaml").read_text())
        generatoren = kustomization["configMapGenerator"]
        bestanden = [f for g in generatoren for f in g["files"]]
        jars = [f for f in bestanden if f.endswith(".jar")]
        assert len(jars) == 1, f"verwacht precies een jar in de generator, gevonden: {jars}"
        return jars[0]

    def test_de_jar_ligt_er_ook_echt(self) -> None:
        """Ontbreekt hij, dan rendert kustomize niet eens - maar dan pas bij de uitrol."""
        jar = KEYCLOAK_BASE / self._jar_uit_de_generator()
        assert jar.is_file(), f"{jar} ontbreekt"
        assert jar.stat().st_size > 0

    def test_de_jar_past_ruim_in_een_configmap(self) -> None:
        """De grens is 1 MiB. Alles staat op ``provided``, dus er is niets geschaduwd."""
        jar = KEYCLOAK_BASE / self._jar_uit_de_generator()
        assert jar.stat().st_size < 1024 * 1024

    def test_de_initcontainer_kopieert_precies_die_jar(self) -> None:
        naam = Path(self._jar_uit_de_generator()).name
        init = _deployment()["spec"]["template"]["spec"]["initContainers"][0]
        commando = "\n".join(init["command"])
        assert f"cp /zad-providers/{naam} /opt/keycloak/providers/" in commando

    def test_de_local_overlay_kopieert_hem_ook(self) -> None:
        """Die overlay VERVANGT het hele commando van de initContainer.

        Vergeet je de regel daar, dan mist de jar precies op het clustertype waar niemand
        het merkt tot Keycloak weigert te starten.
        """
        naam = Path(self._jar_uit_de_generator()).name
        overlay = (KEYCLOAK_OVERLAYS / "local/kustomization.yaml").read_text()
        assert f"cp /zad-providers/{naam} /opt/keycloak/providers/" in overlay

    def test_de_jar_komt_niet_van_het_netwerk(self) -> None:
        """Aan deze jar hangt een startvlag waarzonder Keycloak weigert te starten.

        Zou hij van github.com komen zoals het thema en de SAML-mapper, dan legt een
        hapering daar het inloggen van het hele platform plat.
        """
        naam = Path(self._jar_uit_de_generator()).name
        init = _deployment()["spec"]["template"]["spec"]["initContainers"][0]
        commando = "\n".join(init["command"])
        for regel in commando.splitlines():
            if naam in regel:
                assert "wget" not in regel and "curl" not in regel


class TestDeRelayInDeOmgeving:
    """De bestemming komt uit de omgeving van de pod, en nergens anders vandaan."""

    def _env(self) -> dict[str, dict]:
        return {e["name"]: e for e in _keycloak_container(_deployment())["env"]}

    def test_elke_variabele_die_de_provider_eist_staat_er(self) -> None:
        """De namen komen uit de Java-bron, niet uit een lijst hier.

        Hernoemt iemand daar een variabele, dan valt deze toets om in plaats van de
        bevestigingsmail: de provider gooit dan pas bij de eerste verzending.
        """
        env = self._env()
        for constante in ("ENV_HOST", "ENV_PORT", "ENV_USERNAME", "ENV_PASSWORD", "ENV_FROM", "ENV_STARTTLS"):
            naam = _java_constante("RelayMailConfig.java", constante)
            assert naam in env, f"{naam} ({constante}) ontbreekt in de Keycloak-deployment"

    def test_het_wachtwoord_komt_uit_een_geheim_en_staat_niet_in_het_manifest(self) -> None:
        wachtwoord = self._env()[_java_constante("RelayMailConfig.java", "ENV_PASSWORD")]
        assert "value" not in wachtwoord
        assert wachtwoord["valueFrom"]["secretKeyRef"]["name"] == "keycloak-mail-credentials"

    def test_een_ontbrekend_geheim_is_geen_bootblokkade(self) -> None:
        """``optional: true``, en dat is een afweging.

        Zonder die vlag start Keycloak NIET zolang het geheim er niet is, en dan blokkeert
        een mailgeheim de hele identiteitsvoorziening van het cluster. Met de vlag start hij,
        logt de provider dat hij geen bruikbare relayconfiguratie heeft, en faalt elke
        verzending luid.
        """
        wachtwoord = self._env()[_java_constante("RelayMailConfig.java", "ENV_PASSWORD")]
        assert wachtwoord["valueFrom"]["secretKeyRef"]["optional"] is True
