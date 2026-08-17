"""De metingen staan op EEN plek: het tabblad Metrics, niet ook op Deployments.

Het blok "Resource Metrics" hing als dienstsectie van de metrics-scraper op het tabblad
Deployments en haalde exact hetzelfde fragment op als het tabblad Metrics
(``/projects/details/<p>/metrics/<d>``, id ``metrics-content-<naam>``). Dezelfde grafieken,
twee keer. Het blok was er eerder dan het tabblad en is bij het toevoegen daarvan blijven
staan.

Dezelfde afweging als bij de backups, die in project-tabs.html.j2 staat opgeschreven: twee
weergaven van dezelfde gegevens lopen uit de pas.

Deze test bewaakt het terugkomen, EN dat de twee meldingen die het blok droeg niet verloren
zijn gegaan - dat is de enige echte prijs van het weghalen.
"""

from pathlib import Path

from opi.services.catalog.events import EVENT_MARKER
from opi.services.catalog.metrics_scraper import MetricsScraperService
from opi.services.services_enums import UIEvent

OPI = Path(__file__).resolve().parent.parent / "opi"


def _haken_op(service: object, event: UIEvent) -> list[str]:
    """De methoden van ``service`` die op ``event`` hangen.

    Het merk wordt uit ``events.EVENT_MARKER`` gehaald en niet als tekst herhaald: een
    zelfverzonnen attribuutnaam vindt nooit iets, en dan slaagt de test hieronder leeg.
    Dat is precies wat er bij het schrijven ervan gebeurde.
    """
    gevonden = []
    for naam in dir(service):
        merk = getattr(getattr(service, naam, None), EVENT_MARKER, None)
        if merk and merk[0] is event:
            gevonden.append(naam)
    return gevonden


def test_de_haak_wordt_werkelijk_herkend() -> None:
    """Eerst bewijzen dat de meting iets KAN vinden, anders bewijst de test hieronder niets.

    Een lezer die een dienstsectie terugzet, zet hem terug met precies deze decorator; als
    ``_haken_op`` die niet ziet, staat de test hieronder groen bij een blok dat gewoon
    gerenderd wordt.
    """
    from opi.services.catalog.events import on

    class _Proef:
        @on(UIEvent.DEPLOYMENT_SECTIONS)
        def blok(self, ctx: object) -> list:
            return []

    assert _haken_op(_Proef(), UIEvent.DEPLOYMENT_SECTIONS) == ["blok"]
    assert _haken_op(_Proef(), UIEvent.PROJECT_SECTIONS) == []


def test_de_metrics_scraper_levert_geen_deploymentsectie_meer() -> None:
    """Zonder deze test komt het blok terug zodra iemand de haak weer aanhaakt."""
    haken = _haken_op(MetricsScraperService(), UIEvent.DEPLOYMENT_SECTIONS)

    assert haken == [], f"de metrics-scraper hangt weer op DEPLOYMENT_SECTIONS: {haken}"


def test_het_dienstsjabloon_is_weg_en_wordt_nergens_meer_opgenomen() -> None:
    """Een verweesd sjabloon is de tweede weg terug: iemand neemt het ergens anders op."""
    sjabloon = OPI / "services/catalog/metrics_scraper/section-deployment.html.j2"
    assert not sjabloon.exists(), "het dienstsjabloon staat er weer"

    # Alleen in commentaar mag de naam nog voorkomen (die legt uit waarom hij weg is).
    for pad in (OPI / "services/catalog/metrics_scraper/__init__.py",):
        assert 'template="metrics_scraper/section-deployment.html.j2"' not in pad.read_text(), (
            f"{pad} verwijst weer naar het verwijderde sjabloon"
        )


def test_de_melding_over_een_ander_cluster_staat_nu_bij_de_grafieken() -> None:
    """De enige melding die het dienstblok droeg en het fragment nog niet had.

    Prometheus-onbereikbaar vangt het fragment zelf al af (``prometheus_bereikbaar``). Een
    deployment op een ander cluster niet: die leverde zes lege grafieken zonder reden, en
    dan zoekt de lezer de fout bij zijn eigen applicatie.
    """
    fragment = (OPI / "templates_lotc/bg/_deployment-metrics.html.j2").read_text()

    assert "ander_cluster" in fragment, "de melding over een ander cluster staat er niet"
    # Voor de "nog geen metingen"-tak, want dat is bij een vreemd cluster de verkeerde uitleg.
    assert fragment.index("ander_cluster") < fragment.index("metingen_leeg"), (
        "de clustermelding staat na 'nog geen metingen' en wordt dus nooit getoond"
    )
