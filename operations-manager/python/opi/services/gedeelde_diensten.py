"""Hoe vol en hoe druk de GEDEELDE diensten zijn: de PVC's en de databases.

Dit is de meetlaag onder /admin/diensten. Hij doet drie dingen en niet meer:

1. hij houdt de DREMPELS op een plek (``DREMPELS``), zodat de volgende stap - alerting -
   dezelfde grenzen kan gebruiken en er geen tweede waarheid ontstaat;
2. hij vraagt per blok EEN set queries op die alle reeksen tegelijk teruggeeft, nooit een
   query per rij (het projectdashboard doet 132 aanroepen per weergave en duurt daardoor
   seconden - dat schaalt mee met het platform, en dat willen we hier niet);
3. hij houdt "niets te melden" en "kon niet meten" UIT ELKAAR. Op het dashboard werd een
   mislukte meting op DEBUG gelogd; een kapotte grafiek zag er daardoor precies zo uit als
   "geen verkeer", en dat is maanden onopgemerkt gebleven. Hier gaat een mislukte meting
   op WARNING de log in EN komt hij als ``fout`` op het scherm.

DE BRON IS DE CONNECTOR, NIET EEN URL. In productie staat ``METRICS_BACKEND=grafana`` en
loopt alles via Grafana naar Mimir; onze eigen Prometheus in rig-prd-operations heeft de
volume- en containermetrieken helemaal niet. ``get_metrics_connector()`` kiest de juiste
bron per omgeving, dus alles hier gaat daardoorheen.

WAT ER NIET IN STAAT, EN WAAROM NIET

Redis en MinIO hebben geen exporter: van Redis zijn alleen ``argocd_redis_*`` beschikbaar
(de clientmetrieken van ArgoCD, niet de toestand van onze Redis), en van MinIO is er nul.
Die staan in ``ONGEMETEN_DIENSTEN`` en worden op de pagina BENOEMD. Een leeg vak zou als
"goed" lezen, en drie lege kaarten lezen als een kapotte pagina.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from opi.connectors.prometheus import get_metrics_connector

logger = logging.getLogger(__name__)

#: De toestand van een gemeten waarde. In oplopende ernst; ``_ERNST`` hieronder legt die
#: volgorde vast zodat een rij de zwaarste van zijn waarden kan overnemen.
STATUS_ONBEKEND = "onbekend"
STATUS_OK = "ok"
STATUS_WAARSCHUWING = "waarschuwing"
STATUS_KRITIEK = "kritiek"

_ERNST: dict[str, int] = {
    STATUS_ONBEKEND: 0,
    STATUS_OK: 1,
    STATUS_WAARSCHUWING: 2,
    STATUS_KRITIEK: 3,
}


@dataclass(frozen=True)
class Drempel:
    """Vanaf welke waarde iets opvalt, en vanaf welke waarde het niet meer kan wachten.

    ``eenheid`` en ``uitleg`` zitten er niet voor de sier bij: de pagina toont de drempels
    zelf, en dan hoeft niemand in de code te kijken om te weten waar een rood vlak vandaan
    komt.
    """

    naam: str
    waarschuwing: float
    kritiek: float
    eenheid: str
    uitleg: str


#: DE drempels. Een plek, want de volgende stap is alerting en die moet dezelfde grenzen
#: gebruiken; twee lijstjes lopen gegarandeerd uiteen.
#:
#: De getallen zijn bewust rond en aan de voorzichtige kant. Ze zijn geen meting maar een
#: keuze, en een keuze die je kunt zien staan is er een waarover je het oneens kunt zijn.
DREMPELS: dict[str, Drempel] = {
    "pvc_vulling": Drempel(
        naam="pvc_vulling",
        waarschuwing=75.0,
        kritiek=85.0,
        eenheid="%",
        uitleg="Hoeveel van een PVC gebruikt is. Een volle PVC laat de pod niet meer schrijven.",
    ),
    "pvc_inodes": Drempel(
        naam="pvc_inodes",
        waarschuwing=75.0,
        kritiek=85.0,
        eenheid="%",
        uitleg="Hoeveel inodes gebruikt zijn. Een volle inodetabel geeft 'no space left' terwijl de bytes meevallen.",
    ),
    "verbindingen_wachtend": Drempel(
        naam="verbindingen_wachtend",
        waarschuwing=1.0,
        kritiek=5.0,
        eenheid="verbindingen",
        uitleg="Verbindingen die op een lock staan te wachten. Boven nul is er iets aan het knellen.",
    ),
    "langste_transactie": Drempel(
        naam="langste_transactie",
        waarschuwing=300.0,
        kritiek=3600.0,
        eenheid="seconden",
        uitleg="De langstlopende transactie. Een transactie die blijft hangen houdt vacuum tegen.",
    ),
    "xid_leeftijd": Drempel(
        naam="xid_leeftijd",
        waarschuwing=150_000_000.0,
        kritiek=500_000_000.0,
        eenheid="transacties",
        uitleg="Hoe ver de oudste transactie-id achterloopt. Bij 2 miljard stopt PostgreSQL met schrijven.",
    ),
}


@dataclass(frozen=True)
class OngemetenDienst:
    """Een gedeelde dienst waarvan we de toestand NIET kunnen zien, en wat dat zou kosten."""

    naam: str
    reden: str
    nodig: str


#: Wat er niet in beeld is. Staat op de pagina zelf: afwezigheid mag niet als "goed" lezen.
ONGEMETEN_DIENSTEN: list[OngemetenDienst] = [
    OngemetenDienst(
        naam="Redis",
        reden=(
            "Er zijn alleen argocd_redis_*-metrieken, en dat zijn de clientmetrieken van "
            "ArgoCD - niet de toestand van onze Redis zelf."
        ),
        nodig="Een redis-exporter naast Redis, plus een scrape-config. Die keuze ligt bij het platformteam.",
    ),
    OngemetenDienst(
        naam="MinIO",
        reden=(
            "Niet in beeld via de bron die de blokken hierboven gebruiken. Onze EIGEN "
            "Prometheus heeft wel een scrape-job 'minio' op /minio/v2/metrics/cluster; "
            "of daar bruikbare reeksen uit komen is nog niet nagemeten."
        ),
        nodig=(
            "Nameten wat die job werkelijk oplevert, en zo ja er een blok van maken langs "
            "dezelfde weg als Keycloak (rechtstreeks PrometheusConnector)."
        ),
    ),
]


# Per blok een handvol queries die ELK alle reeksen in een keer teruggeven. Nooit een
# query per PVC of per database: dat schaalt lineair mee met het platform.
#
# De sum/max-omhulling is er om de LABELS vast te leggen. Kaal geven deze metrieken ook
# instance, job, usename en state terug, en dan zou een rij per (state, usename) ontstaan
# in plaats van een rij per database.
# LAST_OVER_TIME OVERAL, OM DEZELFDE REDEN ALS BIJ KEYCLOAK. Een instant-query toont alleen
# samples binnen het staleness-venster van vijf minuten. Een deel van deze reeksen komt uit
# een job die elke TWEE UUR scrapet, en die is daarmee bijna altijd onzichtbaar. Gemeten
# tegen de sandbox op 18 augustus 2026: van cnpg_pg_database_size_bytes staan 161 reeksen in
# de index en gaf de kale query er 18 terug. De tabel toonde dus stilzwijgend een deel, en
# dat is niet van een volledige tabel te onderscheiden.
#
# Zes uur terugkijken overleeft ook een gemiste scrape. De prijs is dat een getal tot zes uur
# oud kan zijn. Voor vulling en groottes is dat prima; voor de langste transactie betekent
# het dat je naar de piek van het afgelopen venster kijkt en niet naar dit moment.
_TERUGBLIK = "6h"

# MAX BY EN NIET SUM BY, en dat is geen smaak. Dezelfde target wordt door TWEE jobs
# gescrapet: cnpg_pg_database_size_bytes komt zowel uit 'cloudnative-pg' als uit
# 'kubernetes-pods'. Dat viel niet op zolang er maar een van de twee vers was, want dan
# telde de som een enkele reeks op. Zodra last_over_time ze allebei zichtbaar maakt, telt
# sum ze bij elkaar op: gemeten op de sandbox stond de keycloak-database op 92 MB en maakte
# de som er 183 MB van.
#
# Per (namespace, pod, datname) bestaat er logisch EEN reeks, en per (namespace, pvc) ook.
# Er valt dus niets op te tellen; max neemt de waarde en is ongevoelig voor hoeveel jobs
# hem toevallig scrapen.

_OPSLAG_QUERIES: dict[str, str] = {
    "vulling": (
        "100 * max by (namespace, persistentvolumeclaim) (last_over_time(kubelet_volume_stats_used_bytes[6h]))"
        " / max by (namespace, persistentvolumeclaim) (last_over_time(kubelet_volume_stats_capacity_bytes[6h]))"
    ),
    "gebruikt": "max by (namespace, persistentvolumeclaim) (last_over_time(kubelet_volume_stats_used_bytes[6h]))",
    "capaciteit": "max by (namespace, persistentvolumeclaim) (last_over_time(kubelet_volume_stats_capacity_bytes[6h]))",
    "inodes": (
        "100 * max by (namespace, persistentvolumeclaim) (last_over_time(kubelet_volume_stats_inodes_used[6h]))"
        " / clamp_min("
        "max by (namespace, persistentvolumeclaim) (last_over_time(kubelet_volume_stats_inodes_used[6h]))"
        " + max by (namespace, persistentvolumeclaim) (last_over_time(kubelet_volume_stats_inodes_free[6h]))"
        ", 1)"
    ),
}

_DATABASE_QUERIES: dict[str, str] = {
    "grootte": "max by (namespace, pod, datname) (last_over_time(cnpg_pg_database_size_bytes[6h]))",
    "verbindingen": "max by (namespace, pod, datname) (last_over_time(cnpg_backends_total[6h]))",
    "langste_transactie": "max by (namespace, pod, datname) (last_over_time(cnpg_backends_max_tx_duration_seconds[6h]))",
    "xid_leeftijd": "max by (namespace, pod, datname) (last_over_time(cnpg_pg_database_xid_age[6h]))",
    # cnpg_backends_waiting_total heeft GEEN datname: wachtende verbindingen zijn een
    # eigenschap van de instantie, niet van een database. Vandaar een eigen tabel.
    "wachtend": "max by (namespace, pod) (last_over_time(cnpg_backends_waiting_total[6h]))",
}


# Keycloak komt uit een ANDERE bron dan de blokken hierboven. De kubelet- en CNPG-cijfers
# komen op productie uit Mimir via de Grafana-connector; deze metrieken worden gescrapet
# door onze EIGEN Prometheus (job ``keycloak-rig-metrics``, zie de scrape-config in
# infrastructure/bootstrap/infrastructure/prometheus). Ze zitten niet in Mimir, dus
# ``get_metrics_connector()`` vindt ze niet en dit blok praat rechtstreeks met
# ``PrometheusConnector`` -- net als de metrics-explorer al deed.
#
# De metrieken zelf komen van onze eigen Keycloak-extensie op /realms/master/rig-metrics
# (features/keycloak-rig-metrics.md), niet van Keycloak zelf.
#
# Die job scrapet elke TWEE UUR. Aantallen kloppen daarmee prima; een venster korter dan
# een paar uur levert niets op, en daarom staat er 24h onder de logins en niet 1h.
# LAST_OVER_TIME OP DE GAUGES, EN DAT IS GEEN VERSIERING. Prometheus laat een instant-query
# alleen samples zien die binnen het staleness-venster van vijf minuten vallen. Deze job
# scrapet elke TWEE UUR, dus een kale ``rig_keycloak_users_total`` geeft ongeveer vier
# procent van de tijd een antwoord en de rest van de tijd niets. Gemeten tegen de
# sandbox-Prometheus op 18 augustus 2026: vlak na een scrape kwamen alle realms terug,
# een half uur later nul reeksen, met een gezonde target en de reeksen gewoon in de TSDB.
# Op het scherm stond dan "Er zijn geen Keycloak-metrieken gevonden" -- precies de stille
# storing die deze pagina moet voorkomen.
#
# Het venster is zes uur en niet drie: dan overleeft het beeld ook een gemiste scrape. De
# prijs is dat een getal tot zes uur oud kan zijn, en voor gebruikersaantallen is dat
# prima.
#
# De counters hebben dit NIET nodig. Een range-selector als [24h] valt buiten de
# staleness-regel: die kijkt zelf terug en vindt zijn samples wel.
_KEYCLOAK_QUERIES: dict[str, str] = {
    "realms": "last_over_time(rig_keycloak_realms_total[6h])",
    "gebruikers": "sum by (realm) (last_over_time(rig_keycloak_users_total[6h]))",
    "gebruikers_per_idp": "sum by (realm, idp_type) (last_over_time(rig_keycloak_users_by_idp_total[6h]))",
    "logins": "sum by (realm) (increase(rig_keycloak_logins_total[24h]))",
    "mislukte_logins": "sum by (realm) (increase(rig_keycloak_login_errors_total[24h]))",
}


# CPU, geheugen en opslag van de namespaces waarin WIJ onze diensten draaien.
#
# HIER STAAT WEL EEN NAMESPACEFILTER IN DE PROMQL, en dat gaat niet in tegen de reden om
# hem bij opslag en databases weg te laten. Daar zou een filter de lijst van ALLE projecten
# in elke query bakken en weglopen zodra er een project bij komt. Deze lijst is een korte,
# vaste, per cluster ingestelde opsomming (``service_namespaces`` in cluster_config) die
# niet meegroeit met het platform. Filteren is hier dus juist goedkoper dan alles ophalen
# en de projecten er in Python weer aftrekken.
#
# GEVRAAGD STAAT ERBIJ, EN DAT IS DE HELE REDEN VOOR DIT BLOK. ODCN factureert geheugen als
# request + clamp_min(gebruik - request, 0): per pod het hoogste van gevraagd en gebruikt.
# Gebruik alleen zegt dus niets over de rekening, en een namespace die ruim vraagt en weinig
# gebruikt is hier meteen te zien.
#
# De containermetrieken zonder last_over_time: cadvisor wordt op een normaal interval
# gescrapet, anders dan de tweeuurlijkse jobs onder de blokken hierboven. De volumecijfers
# krijgen het WEL, om precies de reden die bij _OPSLAG_QUERIES staat.
_RESOURCE_QUERIES: dict[str, str] = {
    "cpu_gebruikt": (
        'sum by (namespace) (rate(container_cpu_usage_seconds_total{{namespace=~"{namespaces}",container!=""}}[5m]))'
    ),
    "cpu_gevraagd": 'sum by (namespace) (kube_pod_container_resource_requests{{namespace=~"{namespaces}",resource="cpu"}})',
    "cpu_limiet": 'sum by (namespace) (kube_pod_container_resource_limits{{namespace=~"{namespaces}",resource="cpu"}})',
    "geheugen_gebruikt": (
        'sum by (namespace) (container_memory_working_set_bytes{{namespace=~"{namespaces}",container!=""}})'
    ),
    "geheugen_gevraagd": (
        'sum by (namespace) (kube_pod_container_resource_requests{{namespace=~"{namespaces}",resource="memory"}})'
    ),
    "geheugen_limiet": (
        'sum by (namespace) (kube_pod_container_resource_limits{{namespace=~"{namespaces}",resource="memory"}})'
    ),
    # max by (namespace, persistentvolumeclaim) binnen de som, om dezelfde reden als bij
    # _OPSLAG_QUERIES: dezelfde PVC wordt door twee jobs gescrapet en een kale sum telt hem
    # dan dubbel.
    "opslag_gebruikt": (
        "sum by (namespace) (max by (namespace, persistentvolumeclaim) ("
        'last_over_time(kubelet_volume_stats_used_bytes{{namespace=~"{namespaces}"}}[6h])))'
    ),
    "opslag_capaciteit": (
        "sum by (namespace) (max by (namespace, persistentvolumeclaim) ("
        'last_over_time(kubelet_volume_stats_capacity_bytes{{namespace=~"{namespaces}"}}[6h])))'
    ),
}


@dataclass(frozen=True)
class ResourceRij:
    """Wat een servicenamespace vraagt, gebruikt en mag.

    GEEN status, om dezelfde reden als bij :class:`RealmRij`: op namespaceniveau is er geen
    grens die ergens op slaat. Een namespace telt tientallen pods bij elkaar op, en een
    enkele pod die tegen zijn limiet aan zit verdwijnt in die som. De drempel die hier wel
    hoort staat een blok hoger, per PVC.

    De CPU-waarden zijn cores, de geheugen- en opslagwaarden bytes. None is "niet gemeten"
    en nadrukkelijk niet nul: een namespace zonder pods en een namespace waarvan de meting
    mislukte zien er anders uit.
    """

    namespace: str
    cpu_gebruikt: float | None
    cpu_gevraagd: float | None
    cpu_limiet: float | None
    geheugen_gebruikt: float | None
    geheugen_gevraagd: float | None
    geheugen_limiet: float | None
    opslag_gebruikt: float | None
    opslag_capaciteit: float | None


@dataclass(frozen=True)
class OpslagRij:
    """Een PVC."""

    namespace: str
    claim: str
    vulling_procent: float | None
    gebruikt_bytes: float | None
    capaciteit_bytes: float | None
    inodes_procent: float | None
    status: str


@dataclass(frozen=True)
class DatabaseRij:
    """Een database binnen een CNPG-instantie."""

    namespace: str
    instantie: str
    database: str
    grootte_bytes: float | None
    verbindingen: float | None
    langste_transactie_seconden: float | None
    xid_leeftijd: float | None
    status: str


@dataclass(frozen=True)
class RealmRij:
    """Een Keycloak-realm: hoeveel gebruikers erin zitten en hoeveel er inloggen.

    GEEN status, met opzet. De andere blokken hebben een drempel die ergens op slaat: een
    volle PVC loopt vol, een oplopende xid-leeftijd eindigt in een database die niet meer
    schrijft. Bij mislukte logins is er geen zulk getal. Tien mislukte pogingen op een
    realm met drie gebruikers is iets anders dan tien op een realm met duizend, en wat
    "te veel" is hangt af van de aanvalsdruk, niet van ons. Een verzonnen grens zou hier
    groen of rood tonen zonder betekenis, en dat is erger dan geen kleur.
    """

    realm: str
    gebruikers: float | None
    gebruikers_per_idp: dict[str, float]
    logins_24u: float | None
    mislukte_logins_24u: float | None


@dataclass(frozen=True)
class InstantieRij:
    """Een CNPG-instantie, met wat alleen op instantieniveau bestaat."""

    namespace: str
    instantie: str
    verbindingen: float | None
    wachtend: float | None
    status: str


@dataclass
class Blok:
    """De uitkomst van een blok metingen.

    ``gemeten`` en ``fout`` bestaan apart van ``rijen`` omdat een LEGE lijst twee heel
    verschillende dingen kan betekenen: er is niets te melden, of er kon niet gemeten
    worden. Wie dat samenneemt bouwt precies de stille storing die dit overzicht moet
    voorkomen.
    """

    gemeten: bool = False
    fout: str | None = None
    rijen: list[Any] = field(default_factory=list)
    extra_rijen: list[Any] = field(default_factory=list)
    #: Een telling die het blok als geheel betreft en niet uit de rijen volgt. Het
    #: aantal realms komt uit een eigen metriek: een realm zonder gebruikers levert
    #: geen rij op, maar bestaat wel.
    totaal: float | None = None


def beoordeel(drempel_naam: str, waarde: float | None) -> str:
    """Welke toestand hoort bij ``waarde`` volgens de drempel ``drempel_naam``?

    Een waarde die er niet is, is ``onbekend`` en nadrukkelijk niet ``ok``: niet kunnen
    meten is geen goed nieuws.
    """
    if waarde is None:
        return STATUS_ONBEKEND
    drempel = DREMPELS[drempel_naam]
    if waarde >= drempel.kritiek:
        return STATUS_KRITIEK
    if waarde >= drempel.waarschuwing:
        return STATUS_WAARSCHUWING
    return STATUS_OK


def zwaarste(statussen: list[str]) -> str:
    """De ernstigste van een aantal toestanden; ``ok`` als er niets is."""
    if not statussen:
        return STATUS_OK
    return max(statussen, key=lambda status: _ERNST[status])


def _waarde(reeks: dict[str, Any]) -> float | None:
    """De getalswaarde uit een instant-resultaat, of None als hij niet te lezen is.

    Beide connectoren leveren ``{"metric": {...}, "value": [tijdstip, "getal"]}``. NaN
    komt voor bij een deling waarvan de noemer ontbreekt en is geen meting, dus None.
    """
    rauw = reeks.get("value")
    if not rauw or len(rauw) < 2:
        return None
    try:
        getal = float(rauw[1])
    except TypeError, ValueError:
        return None
    if getal != getal:  # NaN
        return None
    return getal


def _op_labels(resultaten: list[dict[str, Any]], labels: tuple[str, ...]) -> dict[tuple[str, ...], float]:
    """Zet een queryresultaat om in een tabel op de gevraagde labels."""
    tabel: dict[tuple[str, ...], float] = {}
    for reeks in resultaten:
        metric = reeks.get("metric", {})
        sleutel = tuple(str(metric.get(label, "")) for label in labels)
        waarde = _waarde(reeks)
        if waarde is not None:
            tabel[sleutel] = waarde
    return tabel


def projectnamespaces() -> set[str]:
    """De namespaces die van GEBRUIKERS zijn, en dus niet op deze pagina horen.

    Deze pagina gaat over de diensten die wij aanbieden: de databases en volumes van het
    ZAD-platform zelf, plus de infrastructuur eromheen. Wat een gebruiker in zijn eigen
    project doet is zijn zaak en staat op de projectpagina.

    De queries hebben GEEN namespacefilter -- ze tellen op wat er is, in een set per blok,
    en dat blijft zo: een filter in PromQL zou een lijst namespaces in elke query bakken en
    die loopt weg zodra er een project bij komt. Er wordt hier dus afgetrokken in plaats van
    gefilterd.

    En het is een uitsluiting, geen lijst van onze eigen namespaces. Zo'n lijst zou drijven:
    komt er een infrastructuuronderdeel bij, dan valt het stil buiten beeld en ziet niemand
    dat. Andersom is veiliger -- een onbekende namespace komt IN beeld, en dat merk je.
    """
    from opi.core.cluster_config import get_namespace_prefix
    from opi.core.config import settings
    from opi.services.project_store import get_project_store

    try:
        prefix = get_namespace_prefix(settings.CLUSTER_MANAGER)
    except ValueError:
        logger.warning("Onbekend cluster %s; projectnamespaces niet af te leiden", settings.CLUSTER_MANAGER)
        return set()

    namespaces: set[str] = set()
    for project in get_project_store().get_all():
        namespaces.add(f"{prefix}{project.name}")
        namespaces.add(f"{prefix}{project.name}-infrastructure")
    return namespaces


async def _voer_queries_uit(queries: dict[str, str]) -> dict[str, list[dict[str, Any]]]:
    """Voer alle queries van een blok tegelijk uit via de metriekconnector."""
    connector = await get_metrics_connector()

    namen = list(queries)
    resultaten = await asyncio.gather(*(connector.custom_query(queries[naam]) for naam in namen))
    return dict(zip(namen, resultaten, strict=True))


def servicenamespaces() -> list[str]:
    """De namespaces waarin het platform zijn eigen diensten draait, voor DIT cluster.

    De tegenhanger van :func:`projectnamespaces`. Daar wordt afgetrokken omdat de lijst
    projecten meegroeit; hier wordt opgesomd omdat de lijst diensten dat niet doet en per
    cluster anders is. Zie ``service_namespaces`` in cluster_config.
    """
    from opi.core.cluster_config import get_service_namespaces
    from opi.core.config import settings

    try:
        return get_service_namespaces(settings.CLUSTER_MANAGER)
    except ValueError:
        logger.warning("Onbekend cluster %s; servicenamespaces niet af te leiden", settings.CLUSTER_MANAGER)
        return []


async def haal_resources() -> Blok:
    """CPU, geheugen en opslag per servicenamespace.

    De rijen komen uit de INGESTELDE lijst en niet uit het queryresultaat. Een namespace die
    niets terugmeet hoort zichtbaar te blijven met streepjes: anders is "hier draait niets"
    niet te onderscheiden van "deze namespace is nooit ingesteld", en dat laatste is precies
    de fout die je wilt zien.
    """
    namespaces = servicenamespaces()
    if not namespaces:
        return Blok(gemeten=False, fout="Voor dit cluster staan er geen service_namespaces in de clusterconfiguratie.")

    # Namespacenamen zijn DNS-labels (kleine letters, cijfers, koppelteken), dus er zit
    # niets in dat in een PromQL-regex een andere betekenis krijgt.
    filter_regex = "|".join(namespaces)
    queries = {naam: sjabloon.format(namespaces=filter_regex) for naam, sjabloon in _RESOURCE_QUERIES.items()}

    try:
        antwoorden = await _voer_queries_uit(queries)
    except Exception as fout:
        # Breed gevangen om dezelfde reden als bij haal_opslag: dit blok mag de pagina niet
        # meenemen in zijn val, en een mislukte meting hoort op WARNING in de log en als
        # fout op het scherm.
        logger.warning("Kon de resourcemetrieken niet ophalen: %s", fout, exc_info=True)
        return Blok(gemeten=False, fout=str(fout))

    tabellen = {naam: _op_labels(antwoorden[naam], ("namespace",)) for naam in queries}

    rijen = [
        ResourceRij(
            namespace=namespace,
            cpu_gebruikt=tabellen["cpu_gebruikt"].get((namespace,)),
            cpu_gevraagd=tabellen["cpu_gevraagd"].get((namespace,)),
            cpu_limiet=tabellen["cpu_limiet"].get((namespace,)),
            geheugen_gebruikt=tabellen["geheugen_gebruikt"].get((namespace,)),
            geheugen_gevraagd=tabellen["geheugen_gevraagd"].get((namespace,)),
            geheugen_limiet=tabellen["geheugen_limiet"].get((namespace,)),
            opslag_gebruikt=tabellen["opslag_gebruikt"].get((namespace,)),
            opslag_capaciteit=tabellen["opslag_capaciteit"].get((namespace,)),
        )
        for namespace in namespaces
    ]

    # De totaalrij is wat het platform bij elkaar aan zichzelf besteedt; die staat als
    # extra_rijen apart, zodat het sjabloon hem onderaan kan zetten zonder hem uit de
    # gewone rijen te moeten herkennen.
    def _totaal(veld: str) -> float | None:
        waarden = [getattr(rij, veld) for rij in rijen if getattr(rij, veld) is not None]
        return sum(waarden) if waarden else None

    totaal = ResourceRij(
        namespace="Totaal",
        cpu_gebruikt=_totaal("cpu_gebruikt"),
        cpu_gevraagd=_totaal("cpu_gevraagd"),
        cpu_limiet=_totaal("cpu_limiet"),
        geheugen_gebruikt=_totaal("geheugen_gebruikt"),
        geheugen_gevraagd=_totaal("geheugen_gevraagd"),
        geheugen_limiet=_totaal("geheugen_limiet"),
        opslag_gebruikt=_totaal("opslag_gebruikt"),
        opslag_capaciteit=_totaal("opslag_capaciteit"),
    )

    return Blok(gemeten=True, rijen=rijen, extra_rijen=[totaal])


async def haal_opslag() -> Blok:
    """De vulling van alle PVC's, volst eerst.

    Dit is het belangrijkste blok: hier komt de eerste echte melding vandaan.
    """
    try:
        antwoorden = await _voer_queries_uit(_OPSLAG_QUERIES)
    except Exception as fout:
        # Breed gevangen, tegen de huisregel in, en met opzet: dit blok mag de PAGINA niet
        # meenemen in zijn val. De connectoren gooien elk hun eigen fout (Prometheus,
        # Grafana) en daar komen httpx- en netwerkfouten doorheen; wie hier alleen de
        # bekende namen vangt, levert bij de eerstvolgende onbekende een 500 op het geheel.
        # WARNING en niet DEBUG: een meting die stilletjes mislukt is niet te
        # onderscheiden van een meting die niets vond.
        logger.warning("Kon de opslagmetrieken niet ophalen: %s", fout, exc_info=True)
        return Blok(gemeten=False, fout=str(fout))

    labels = ("namespace", "persistentvolumeclaim")
    vulling = _op_labels(antwoorden["vulling"], labels)
    gebruikt = _op_labels(antwoorden["gebruikt"], labels)
    capaciteit = _op_labels(antwoorden["capaciteit"], labels)
    inodes = _op_labels(antwoorden["inodes"], labels)

    van_projecten = projectnamespaces()
    rijen: list[OpslagRij] = []
    for sleutel in sorted(set(vulling) | set(capaciteit)):
        namespace, claim = sleutel
        if namespace in van_projecten:
            continue
        vulling_procent = vulling.get(sleutel)
        inodes_procent = inodes.get(sleutel)
        rijen.append(
            OpslagRij(
                namespace=namespace,
                claim=claim,
                vulling_procent=vulling_procent,
                gebruikt_bytes=gebruikt.get(sleutel),
                capaciteit_bytes=capaciteit.get(sleutel),
                inodes_procent=inodes_procent,
                status=zwaarste(
                    [
                        beoordeel("pvc_vulling", vulling_procent),
                        beoordeel("pvc_inodes", inodes_procent),
                    ]
                ),
            )
        )

    # Volst eerst; een PVC zonder meting zakt naar onderen.
    rijen.sort(key=lambda rij: (rij.vulling_procent is None, -(rij.vulling_procent or 0.0)))
    return Blok(gemeten=True, rijen=list(rijen))


async def haal_databases() -> Blok:
    """Grootte, verbindingen, wachtende verbindingen en langste transactie per database.

    ``rijen`` zijn de databases, ``extra_rijen`` de instanties: wachtende verbindingen
    bestaan alleen per instantie (cnpg_backends_waiting_total heeft geen datname), en die
    op een databaserij plakken zou hetzelfde getal zoveel keer herhalen als er databases
    zijn.
    """
    try:
        antwoorden = await _voer_queries_uit(_DATABASE_QUERIES)
    except Exception as fout:
        # Zie haal_opslag: breed gevangen zodat een onbereikbare metriekbron dit blok kost
        # en niet de pagina, en op WARNING zodat het niet stil gebeurt.
        logger.warning("Kon de databasemetrieken niet ophalen: %s", fout, exc_info=True)
        return Blok(gemeten=False, fout=str(fout))

    db_labels = ("namespace", "pod", "datname")
    grootte = _op_labels(antwoorden["grootte"], db_labels)
    verbindingen = _op_labels(antwoorden["verbindingen"], db_labels)
    langste = _op_labels(antwoorden["langste_transactie"], db_labels)
    xid = _op_labels(antwoorden["xid_leeftijd"], db_labels)

    van_projecten = projectnamespaces()
    rijen: list[DatabaseRij] = []
    for sleutel in sorted(set(grootte) | set(verbindingen)):
        namespace, instantie, database = sleutel
        if namespace in van_projecten:
            continue
        langste_transactie = langste.get(sleutel)
        xid_leeftijd = xid.get(sleutel)
        rijen.append(
            DatabaseRij(
                namespace=namespace,
                instantie=instantie,
                database=database,
                grootte_bytes=grootte.get(sleutel),
                verbindingen=verbindingen.get(sleutel),
                langste_transactie_seconden=langste_transactie,
                xid_leeftijd=xid_leeftijd,
                status=zwaarste(
                    [
                        beoordeel("langste_transactie", langste_transactie),
                        beoordeel("xid_leeftijd", xid_leeftijd),
                    ]
                ),
            )
        )
    rijen.sort(key=lambda rij: -(rij.grootte_bytes or 0.0))

    instantie_labels = ("namespace", "pod")
    wachtend = _op_labels(antwoorden["wachtend"], instantie_labels)
    verbindingen_per_instantie: dict[tuple[str, ...], float] = {}
    for (namespace, instantie, _), waarde in verbindingen.items():
        sleutel = (namespace, instantie)
        verbindingen_per_instantie[sleutel] = verbindingen_per_instantie.get(sleutel, 0.0) + waarde

    instanties: list[InstantieRij] = []
    for sleutel in sorted(set(wachtend) | set(verbindingen_per_instantie)):
        namespace, instantie = sleutel
        if namespace in van_projecten:
            continue
        wachtende = wachtend.get(sleutel)
        instanties.append(
            InstantieRij(
                namespace=namespace,
                instantie=instantie,
                verbindingen=verbindingen_per_instantie.get(sleutel),
                wachtend=wachtende,
                status=beoordeel("verbindingen_wachtend", wachtende),
            )
        )

    return Blok(gemeten=True, rijen=list(rijen), extra_rijen=list(instanties))


async def haal_keycloak() -> Blok:
    """Realms, gebruikers per realm en de logins van de afgelopen 24 uur.

    Praat RECHTSTREEKS met onze eigen Prometheus en niet via ``get_metrics_connector()``:
    zie de toelichting bij ``_KEYCLOAK_QUERIES``. Dat is geen slordigheid maar de enige
    bron die deze metrieken heeft.
    """
    from opi.connectors.prometheus import PrometheusConnector

    try:
        prom = PrometheusConnector()
        namen = list(_KEYCLOAK_QUERIES)
        uitkomsten = await asyncio.gather(*(prom.custom_query(_KEYCLOAK_QUERIES[naam]) for naam in namen))
        antwoorden = dict(zip(namen, uitkomsten, strict=True))
    except Exception as fout:
        # Zie haal_opslag: breed gevangen zodat dit blok valt en niet de pagina, en op
        # WARNING zodat een mislukte meting niet op "niets te melden" lijkt.
        logger.warning("Kon de Keycloak-metrieken niet ophalen: %s", fout, exc_info=True)
        return Blok(gemeten=False, fout=str(fout))

    gebruikers = _op_labels(antwoorden["gebruikers"], ("realm",))
    logins = _op_labels(antwoorden["logins"], ("realm",))
    mislukt = _op_labels(antwoorden["mislukte_logins"], ("realm",))
    per_idp = _op_labels(antwoorden["gebruikers_per_idp"], ("realm", "idp_type"))

    idp_per_realm: dict[str, dict[str, float]] = {}
    for (realm, idp_type), waarde in per_idp.items():
        idp_per_realm.setdefault(realm, {})[idp_type] = waarde

    rijen: list[RealmRij] = []
    for (realm,) in sorted(set(gebruikers) | set(logins)):
        mislukte = mislukt.get((realm,))
        rijen.append(
            RealmRij(
                realm=realm,
                gebruikers=gebruikers.get((realm,)),
                gebruikers_per_idp=idp_per_realm.get(realm, {}),
                logins_24u=logins.get((realm,)),
                mislukte_logins_24u=mislukte,
            )
        )

    # Meeste gebruikers eerst; een realm zonder meting zakt naar onderen.
    rijen.sort(key=lambda rij: (rij.gebruikers is None, -(rij.gebruikers or 0.0)))

    realms_totaal = _op_labels(antwoorden["realms"], ())
    return Blok(gemeten=True, rijen=list(rijen), totaal=realms_totaal.get(()))
