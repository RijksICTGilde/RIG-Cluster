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
_OPSLAG_QUERIES: dict[str, str] = {
    "vulling": (
        "100 * sum by (namespace, persistentvolumeclaim) (kubelet_volume_stats_used_bytes)"
        " / sum by (namespace, persistentvolumeclaim) (kubelet_volume_stats_capacity_bytes)"
    ),
    "gebruikt": "sum by (namespace, persistentvolumeclaim) (kubelet_volume_stats_used_bytes)",
    "capaciteit": "sum by (namespace, persistentvolumeclaim) (kubelet_volume_stats_capacity_bytes)",
    "inodes": (
        "100 * sum by (namespace, persistentvolumeclaim) (kubelet_volume_stats_inodes_used)"
        " / clamp_min("
        "sum by (namespace, persistentvolumeclaim) (kubelet_volume_stats_inodes_used)"
        " + sum by (namespace, persistentvolumeclaim) (kubelet_volume_stats_inodes_free)"
        ", 1)"
    ),
}

_DATABASE_QUERIES: dict[str, str] = {
    "grootte": "sum by (namespace, pod, datname) (cnpg_pg_database_size_bytes)",
    "verbindingen": "sum by (namespace, pod, datname) (cnpg_backends_total)",
    "langste_transactie": "max by (namespace, pod, datname) (cnpg_backends_max_tx_duration_seconds)",
    "xid_leeftijd": "max by (namespace, pod, datname) (cnpg_pg_database_xid_age)",
    # cnpg_backends_waiting_total heeft GEEN datname: wachtende verbindingen zijn een
    # eigenschap van de instantie, niet van een database. Vandaar een eigen tabel.
    "wachtend": "sum by (namespace, pod) (cnpg_backends_waiting_total)",
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
