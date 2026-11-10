"""How full and how busy the SHARED services are: the PVCs and the databases.

The measuring layer under /admin/diensten. It does three things and no more:

1. it keeps the thresholds in one place (``THRESHOLDS``), so alerting can use the same
   limits and no second truth arises;
2. it asks ONE set of queries per block that returns every series at once, never a query
   per row: a query per row scales linearly with the platform;
3. it keeps "nothing to report" and "could not measure" APART. A failed measurement goes
   into the log at WARNING and reaches the screen as ``fout``. Logged at DEBUG, a broken
   graph looks exactly like "no traffic".

THE SOURCE IS THE CONNECTOR, NOT A URL. On production ``METRICS_BACKEND=grafana`` routes
everything through Grafana to Mimir, and our own Prometheus in rig-prd-operations does not
have the volume and container metrics at all. ``get_metrics_connector()`` picks the right
source per environment, so everything here goes through it.

Redis and MinIO have no exporter: of Redis only ``argocd_redis_*`` is available (ArgoCD's
client metrics, not the state of our own Redis), and of MinIO there is nothing. They sit in
``UNMEASURED_SERVICES`` and are NAMED on the page: an empty box would read as "fine", and
three empty cards read as a broken page.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from opi.connectors.prometheus import get_metrics_connector

logger = logging.getLogger(__name__)

#: The state of a measured value, in rising severity. ``_SEVERITY`` below fixes that order,
#: so a row can take over the worst of its own values.
STATUS_ONBEKEND = "onbekend"
STATUS_OK = "ok"
STATUS_WAARSCHUWING = "waarschuwing"
STATUS_KRITIEK = "kritiek"

_SEVERITY: dict[str, int] = {
    STATUS_ONBEKEND: 0,
    STATUS_OK: 1,
    STATUS_WAARSCHUWING: 2,
    STATUS_KRITIEK: 3,
}


@dataclass(frozen=True)
class Threshold:
    """From which value something stands out, and from which it cannot wait any longer.

    ``eenheid`` and ``uitleg`` are shown on the page itself, so nobody has to read the code
    to see where a red cell comes from.
    """

    naam: str
    waarschuwing: float
    kritiek: float
    eenheid: str
    uitleg: str


#: THE thresholds, in one place, because alerting has to use the same limits and two lists
#: are guaranteed to drift apart. The numbers are a choice, not a measurement.
THRESHOLDS: dict[str, Threshold] = {
    "pvc_vulling": Threshold(
        naam="pvc_vulling",
        waarschuwing=75.0,
        kritiek=85.0,
        eenheid="%",
        uitleg="Hoeveel van een PVC gebruikt is. Een volle PVC laat de pod niet meer schrijven.",
    ),
    "pvc_inodes": Threshold(
        naam="pvc_inodes",
        waarschuwing=75.0,
        kritiek=85.0,
        eenheid="%",
        uitleg="Hoeveel inodes gebruikt zijn. Een volle inodetabel geeft 'no space left' terwijl de bytes meevallen.",
    ),
    "verbindingen_wachtend": Threshold(
        naam="verbindingen_wachtend",
        waarschuwing=1.0,
        kritiek=5.0,
        eenheid="verbindingen",
        uitleg="Verbindingen die op een lock staan te wachten. Boven nul is er iets aan het knellen.",
    ),
    "langste_transactie": Threshold(
        naam="langste_transactie",
        waarschuwing=300.0,
        kritiek=3600.0,
        eenheid="seconden",
        uitleg="De langstlopende transactie. Een transactie die blijft hangen houdt vacuum tegen.",
    ),
    "xid_leeftijd": Threshold(
        naam="xid_leeftijd",
        waarschuwing=150_000_000.0,
        kritiek=500_000_000.0,
        eenheid="transacties",
        uitleg="Hoe ver de oudste transactie-id achterloopt. Bij 2 miljard stopt PostgreSQL met schrijven.",
    ),
}


@dataclass(frozen=True)
class UnmeasuredService:
    """A shared service whose state we CANNOT see, and what seeing it would take."""

    naam: str
    reden: str
    nodig: str


#: What is not in view. Shown on the page itself: absence must not read as "fine".
UNMEASURED_SERVICES: list[UnmeasuredService] = [
    UnmeasuredService(
        naam="Redis",
        reden=(
            "Er zijn alleen argocd_redis_*-metrieken, en dat zijn de clientmetrieken van "
            "ArgoCD - niet de toestand van onze Redis zelf."
        ),
        nodig="Een redis-exporter naast Redis, plus een scrape-config. Die keuze ligt bij het platformteam.",
    ),
    UnmeasuredService(
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


# A handful of queries per block, EACH returning every series at once. Never a query per PVC
# or per database: that scales linearly with the platform.
#
# The sum/max wrapping fixes the LABELS. Bare, these metrics also return instance, job,
# usename and state, which would give a row per (state, usename) instead of per database.
#
# LAST_OVER_TIME EVERYWHERE. An instant query only shows samples inside the five-minute
# staleness window, and part of these series comes from a job that scrapes every TWO HOURS,
# so bare it is almost always invisible: it then shows part of a table in a way that cannot
# be told apart from the whole table. Six hours of lookback also survives a missed scrape.
# The price is that a number can be up to six hours old. Fine for fill and sizes; for the
# longest transaction it means the peak of the past window and not this moment.
_LOOKBACK = "6h"

# MAX BY AND NOT SUM BY. The same target is scraped by TWO jobs:
# cnpg_pg_database_size_bytes comes from both 'cloudnative-pg' and 'kubernetes-pods'. As
# long as only one of them was fresh, the sum added up a single series; the moment
# last_over_time makes both visible, sum adds them together and doubles the number.
#
# Per (namespace, pod, datname) there is logically ONE series, and per (namespace, pvc) too,
# so there is nothing to add up. max takes the value and does not care how many jobs happen
# to scrape it.

_STORAGE_QUERIES: dict[str, str] = {
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
    # cnpg_backends_waiting_total has NO datname: waiting connections are a property of the
    # instance and not of a database, hence a table of their own.
    "wachtend": "max by (namespace, pod) (last_over_time(cnpg_backends_waiting_total[6h]))",
}


# Keycloak comes from a DIFFERENT source than the blocks above. The kubelet and CNPG numbers
# come out of Mimir through the Grafana connector on production; these metrics are scraped by
# our OWN Prometheus (job ``keycloak-rig-metrics``, see the scrape config in
# infrastructure/bootstrap/infrastructure/prometheus). They are not in Mimir, so
# ``get_metrics_connector()`` does not find them and this block talks to
# ``PrometheusConnector`` directly. The metrics themselves come from our own Keycloak
# extension on /realms/master/rig-metrics (features/keycloak-rig-metrics.md).
#
# That job scrapes every TWO HOURS, so a window shorter than a couple of hours yields
# nothing; hence 24h under the logins and not 1h.
#
# LAST_OVER_TIME ON THE GAUGES. An instant query only sees samples inside the five-minute
# staleness window, so a bare ``rig_keycloak_users_total`` answers about four percent of the
# time and gives nothing the rest of it: every realm right after a scrape, zero series half
# an hour later, with a healthy target and the series in the TSDB. The screen then said there
# were no Keycloak metrics, which is exactly the silent failure this page exists to prevent.
# Six hours and not three, so the picture also survives a missed scrape.
#
# The counters do NOT need this. A range selector like [24h] falls outside the staleness rule
# and finds its own samples.
_KEYCLOAK_QUERIES: dict[str, str] = {
    "realms": "last_over_time(rig_keycloak_realms_total[6h])",
    "gebruikers": "sum by (realm) (last_over_time(rig_keycloak_users_total[6h]))",
    "gebruikers_per_idp": "sum by (realm, idp_type) (last_over_time(rig_keycloak_users_by_idp_total[6h]))",
    "logins": "sum by (realm) (increase(rig_keycloak_logins_total[24h]))",
    "mislukte_logins": "sum by (realm) (increase(rig_keycloak_login_errors_total[24h]))",
}


# CPU, memory and storage of the namespaces WE run our own services in.
#
# THESE queries DO carry a namespace filter in the PromQL, unlike storage and databases.
# There, a filter would bake the list of ALL projects into every query and run away as soon
# as a project is added; this list is a short, fixed, per-cluster enumeration
# (``service_namespaces`` in cluster_config) that does not grow with the platform.
#
# REQUESTED IS INCLUDED, and that is the whole reason for this block. ODCN bills memory as
# request + clamp_min(usage - request, 0): per pod the higher of requested and used. Usage
# alone therefore says nothing about the bill.
#
# The container metrics go without last_over_time: cadvisor is scraped on a normal interval,
# unlike the two-hourly jobs under the blocks above. The volume numbers DO get it, for the
# reason stated at _STORAGE_QUERIES.
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
    # max by (namespace, persistentvolumeclaim) inside the sum, for the same reason as at
    # _STORAGE_QUERIES: the same PVC is scraped by two jobs, and a bare sum counts it twice.
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
class ResourceRow:
    """What one service namespace asks for, uses and is allowed.

    NO status, for the same reason as :class:`RealmRow`: at namespace level there is no limit
    that means anything. A namespace adds up dozens of pods, and a single pod against its own
    limit disappears in that sum. The threshold that does belong sits a block higher, per PVC.

    The CPU values are cores, the memory and storage values bytes. None is "not measured" and
    emphatically not zero: a namespace without pods and a namespace whose measurement failed
    must not look the same.
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
class StorageRow:
    """One PVC."""

    namespace: str
    claim: str
    vulling_procent: float | None
    gebruikt_bytes: float | None
    capaciteit_bytes: float | None
    inodes_procent: float | None
    status: str


@dataclass(frozen=True)
class DatabaseRow:
    """One database inside a CNPG instance."""

    namespace: str
    instantie: str
    database: str
    grootte_bytes: float | None
    verbindingen: float | None
    langste_transactie_seconden: float | None
    xid_leeftijd: float | None
    status: str


@dataclass(frozen=True)
class RealmRow:
    """One Keycloak realm: how many users are in it and how many of them sign in.

    NO status, deliberately. The other blocks have a limit that means something: a full PVC
    runs out, a rising xid age ends in a database that stops writing. For failed logins there
    is no such number. Ten failed attempts on a realm with three users is not the same as ten
    on a realm with a thousand, and what counts as "too many" depends on the attack pressure
    and not on us. An invented limit would show green or red without meaning.
    """

    realm: str
    gebruikers: float | None
    gebruikers_per_idp: dict[str, float]
    logins_24u: float | None
    mislukte_logins_24u: float | None


@dataclass(frozen=True)
class InstanceRow:
    """One CNPG instance, with what exists only at instance level."""

    namespace: str
    instantie: str
    verbindingen: float | None
    wachtend: float | None
    status: str


@dataclass
class Block:
    """The outcome of one block of measurements.

    ``gemeten`` and ``fout`` exist apart from ``rijen`` because an EMPTY list can mean two
    very different things: there is nothing to report, or nothing could be measured. Taking
    those together builds the silent failure this page exists to prevent.
    """

    gemeten: bool = False
    fout: str | None = None
    rijen: list[Any] = field(default_factory=list)
    extra_rijen: list[Any] = field(default_factory=list)
    #: A count about the block as a whole that does not follow from the rows. The number of
    #: realms comes from a metric of its own: a realm without users yields no row but exists.
    totaal: float | None = None


def assess(threshold_name: str, value: float | None) -> str:
    """Which state belongs to ``value`` under the named threshold.

    A value that is not there is ``onbekend`` and emphatically not ``ok``: being unable to
    measure is not good news.
    """
    if value is None:
        return STATUS_ONBEKEND
    threshold = THRESHOLDS[threshold_name]
    if value >= threshold.kritiek:
        return STATUS_KRITIEK
    if value >= threshold.waarschuwing:
        return STATUS_WAARSCHUWING
    return STATUS_OK


def worst_status(statuses: list[str]) -> str:
    """The worst of a number of states; ``ok`` when there are none."""
    if not statuses:
        return STATUS_OK
    return max(statuses, key=lambda status: _SEVERITY[status])


def _value(series: dict[str, Any]) -> float | None:
    """The number out of an instant result, or None when it cannot be read.

    Both connectors deliver ``{"metric": {...}, "value": [timestamp, "number"]}``. NaN turns
    up on a division whose denominator is missing and is no measurement, so None.
    """
    raw = series.get("value")
    if not raw or len(raw) < 2:
        return None
    try:
        number = float(raw[1])
    except TypeError, ValueError:
        return None
    if number != number:  # NaN
        return None
    return number


def _by_labels(results: list[dict[str, Any]], labels: tuple[str, ...]) -> dict[tuple[str, ...], float]:
    """Turn a query result into a table keyed on the requested labels."""
    table: dict[tuple[str, ...], float] = {}
    for series in results:
        metric = series.get("metric", {})
        key = tuple(str(metric.get(label, "")) for label in labels)
        value = _value(series)
        if value is not None:
            table[key] = value
    return table


def project_namespaces() -> set[str]:
    """The namespaces that belong to USERS, and so do not belong on this page.

    This page is about the services we offer: the databases and volumes of the ZAD platform
    itself, plus the infrastructure around it.

    The queries carry NO namespace filter: they add up what is there, in one set per block,
    because a filter in PromQL would bake a list of namespaces into every query and run away
    as soon as a project is added. Subtraction here, not filtering.

    And it is an EXCLUSION, not a list of our own namespaces. Such a list would drift: a new
    infrastructure component would fall silently out of view. This way round an unknown
    namespace comes INTO view, which is noticeable.
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


async def _run_queries(queries: dict[str, str]) -> dict[str, list[dict[str, Any]]]:
    """Run every query of one block at once, through the metrics connector."""
    connector = await get_metrics_connector()

    query_names = list(queries)
    results = await asyncio.gather(*(connector.query(queries[query_name]) for query_name in query_names))
    return dict(zip(query_names, results, strict=True))


def service_namespaces() -> list[str]:
    """The namespaces the platform runs its own services in, for THIS cluster.

    The counterpart of :func:`project_namespaces`. There the list is subtracted because the
    number of projects grows; here it is enumerated because the number of services does not,
    and differs per cluster. See ``service_namespaces`` in cluster_config.
    """
    from opi.core.cluster_config import get_service_namespaces
    from opi.core.config import settings

    try:
        return get_service_namespaces(settings.CLUSTER_MANAGER)
    except ValueError:
        logger.warning("Onbekend cluster %s; servicenamespaces niet af te leiden", settings.CLUSTER_MANAGER)
        return []


async def fetch_resources() -> Block:
    """CPU, memory and storage per service namespace.

    The rows come from the CONFIGURED list and not from the query result. A namespace that
    measures nothing back stays visible with dashes, or "nothing runs here" could not be told
    apart from "this namespace was never configured".
    """
    namespaces = service_namespaces()
    if not namespaces:
        return Block(gemeten=False, fout="Voor dit cluster staan er geen service_namespaces in de clusterconfiguratie.")

    # Namespace names are DNS labels (lower case, digits, hyphen), so nothing in them takes
    # on another meaning inside a PromQL regex.
    filter_regex = "|".join(namespaces)
    queries = {
        query_name: query_template.format(namespaces=filter_regex)
        for query_name, query_template in _RESOURCE_QUERIES.items()
    }

    try:
        answers = await _run_queries(queries)
    except Exception as fout:
        # Caught broadly for the same reason as in fetch_storage: this block must not take
        # the page down with it, and a failed measurement belongs at WARNING in the log and
        # as an error on the screen.
        logger.warning("Kon de resourcemetrieken niet ophalen: %s", fout, exc_info=True)
        return Block(gemeten=False, fout=str(fout))

    tables = {query_name: _by_labels(answers[query_name], ("namespace",)) for query_name in queries}

    rijen = [
        ResourceRow(
            namespace=namespace,
            cpu_gebruikt=tables["cpu_gebruikt"].get((namespace,)),
            cpu_gevraagd=tables["cpu_gevraagd"].get((namespace,)),
            cpu_limiet=tables["cpu_limiet"].get((namespace,)),
            geheugen_gebruikt=tables["geheugen_gebruikt"].get((namespace,)),
            geheugen_gevraagd=tables["geheugen_gevraagd"].get((namespace,)),
            geheugen_limiet=tables["geheugen_limiet"].get((namespace,)),
            opslag_gebruikt=tables["opslag_gebruikt"].get((namespace,)),
            opslag_capaciteit=tables["opslag_capaciteit"].get((namespace,)),
        )
        for namespace in namespaces
    ]

    # The total row stands apart in extra_rijen, so the template can put it at the bottom
    # without having to recognise it among the ordinary rows.
    def _total(field_name: str) -> float | None:
        values = [getattr(row, field_name) for row in rijen if getattr(row, field_name) is not None]
        return sum(values) if values else None

    totaal = ResourceRow(
        namespace="Totaal",
        cpu_gebruikt=_total("cpu_gebruikt"),
        cpu_gevraagd=_total("cpu_gevraagd"),
        cpu_limiet=_total("cpu_limiet"),
        geheugen_gebruikt=_total("geheugen_gebruikt"),
        geheugen_gevraagd=_total("geheugen_gevraagd"),
        geheugen_limiet=_total("geheugen_limiet"),
        opslag_gebruikt=_total("opslag_gebruikt"),
        opslag_capaciteit=_total("opslag_capaciteit"),
    )

    return Block(gemeten=True, rijen=rijen, extra_rijen=[totaal])


async def fetch_storage() -> Block:
    """How full every PVC is, fullest first."""
    try:
        answers = await _run_queries(_STORAGE_QUERIES)
    except Exception as fout:
        # Caught broadly, against the house rule, and deliberately: this block must not take
        # the PAGE down with it. Each connector raises its own error (Prometheus, Grafana) and
        # httpx and network errors come through as well, so catching only the known names
        # turns the next unknown one into a 500 on the whole page. WARNING and not DEBUG,
        # because a measurement that fails quietly cannot be told from one that found nothing.
        logger.warning("Kon de opslagmetrieken niet ophalen: %s", fout, exc_info=True)
        return Block(gemeten=False, fout=str(fout))

    labels = ("namespace", "persistentvolumeclaim")
    fill = _by_labels(answers["vulling"], labels)
    used = _by_labels(answers["gebruikt"], labels)
    capacity = _by_labels(answers["capaciteit"], labels)
    inodes = _by_labels(answers["inodes"], labels)

    of_projects = project_namespaces()
    rijen: list[StorageRow] = []
    for key in sorted(set(fill) | set(capacity)):
        namespace, claim = key
        if namespace in of_projects:
            continue
        vulling_procent = fill.get(key)
        inodes_procent = inodes.get(key)
        rijen.append(
            StorageRow(
                namespace=namespace,
                claim=claim,
                vulling_procent=vulling_procent,
                gebruikt_bytes=used.get(key),
                capaciteit_bytes=capacity.get(key),
                inodes_procent=inodes_procent,
                status=worst_status(
                    [
                        assess("pvc_vulling", vulling_procent),
                        assess("pvc_inodes", inodes_procent),
                    ]
                ),
            )
        )

    # Fullest first; a PVC without a measurement sinks to the bottom.
    rijen.sort(key=lambda row: (row.vulling_procent is None, -(row.vulling_procent or 0.0)))
    return Block(gemeten=True, rijen=list(rijen))


async def fetch_databases() -> Block:
    """Size, connections, waiting connections and longest transaction per database.

    ``rijen`` are the databases, ``extra_rijen`` the instances: waiting connections exist
    only per instance (cnpg_backends_waiting_total has no datname), and putting them on a
    database row would repeat the same number once per database.
    """
    try:
        answers = await _run_queries(_DATABASE_QUERIES)
    except Exception as fout:
        # See fetch_storage: caught broadly so an unreachable metrics source costs this block
        # and not the page, and at WARNING so it does not happen quietly.
        logger.warning("Kon de databasemetrieken niet ophalen: %s", fout, exc_info=True)
        return Block(gemeten=False, fout=str(fout))

    db_labels = ("namespace", "pod", "datname")
    sizes = _by_labels(answers["grootte"], db_labels)
    verbindingen = _by_labels(answers["verbindingen"], db_labels)
    longest = _by_labels(answers["langste_transactie"], db_labels)
    xid = _by_labels(answers["xid_leeftijd"], db_labels)

    of_projects = project_namespaces()
    rijen: list[DatabaseRow] = []
    for key in sorted(set(sizes) | set(verbindingen)):
        namespace, instantie, database = key
        if namespace in of_projects:
            continue
        longest_transaction = longest.get(key)
        xid_leeftijd = xid.get(key)
        rijen.append(
            DatabaseRow(
                namespace=namespace,
                instantie=instantie,
                database=database,
                grootte_bytes=sizes.get(key),
                verbindingen=verbindingen.get(key),
                langste_transactie_seconden=longest_transaction,
                xid_leeftijd=xid_leeftijd,
                status=worst_status(
                    [
                        assess("langste_transactie", longest_transaction),
                        assess("xid_leeftijd", xid_leeftijd),
                    ]
                ),
            )
        )
    rijen.sort(key=lambda row: -(row.grootte_bytes or 0.0))

    instance_labels = ("namespace", "pod")
    wachtend = _by_labels(answers["wachtend"], instance_labels)
    connections_per_instance: dict[tuple[str, ...], float] = {}
    for (namespace, instantie, _), value in verbindingen.items():
        key = (namespace, instantie)
        connections_per_instance[key] = connections_per_instance.get(key, 0.0) + value

    instances: list[InstanceRow] = []
    for key in sorted(set(wachtend) | set(connections_per_instance)):
        namespace, instantie = key
        if namespace in of_projects:
            continue
        waiting_count = wachtend.get(key)
        instances.append(
            InstanceRow(
                namespace=namespace,
                instantie=instantie,
                verbindingen=connections_per_instance.get(key),
                wachtend=waiting_count,
                status=assess("verbindingen_wachtend", waiting_count),
            )
        )

    return Block(gemeten=True, rijen=list(rijen), extra_rijen=list(instances))


async def fetch_keycloak() -> Block:
    """Realms, users per realm and the logins of the past 24 hours.

    Talks DIRECTLY to our own Prometheus and not through ``get_metrics_connector()``: see the
    note at ``_KEYCLOAK_QUERIES``. It is the only source that has these metrics.
    """
    from opi.connectors.prometheus import PrometheusConnector

    try:
        prom = PrometheusConnector()
        query_names = list(_KEYCLOAK_QUERIES)
        outcomes = await asyncio.gather(*(prom.query(_KEYCLOAK_QUERIES[query_name]) for query_name in query_names))
        answers = dict(zip(query_names, outcomes, strict=True))
    except Exception as fout:
        # See fetch_storage: caught broadly so this block falls and not the page, and at
        # WARNING so a failed measurement does not look like "nothing to report".
        logger.warning("Kon de Keycloak-metrieken niet ophalen: %s", fout, exc_info=True)
        return Block(gemeten=False, fout=str(fout))

    gebruikers = _by_labels(answers["gebruikers"], ("realm",))
    logins = _by_labels(answers["logins"], ("realm",))
    failed = _by_labels(answers["mislukte_logins"], ("realm",))
    per_idp = _by_labels(answers["gebruikers_per_idp"], ("realm", "idp_type"))

    idp_per_realm: dict[str, dict[str, float]] = {}
    for (realm, idp_type), value in per_idp.items():
        idp_per_realm.setdefault(realm, {})[idp_type] = value

    rijen: list[RealmRow] = []
    for (realm,) in sorted(set(gebruikers) | set(logins)):
        failed_count = failed.get((realm,))
        rijen.append(
            RealmRow(
                realm=realm,
                gebruikers=gebruikers.get((realm,)),
                gebruikers_per_idp=idp_per_realm.get(realm, {}),
                logins_24u=logins.get((realm,)),
                mislukte_logins_24u=failed_count,
            )
        )

    # Most users first; a realm without a measurement sinks to the bottom.
    rijen.sort(key=lambda row: (row.gebruikers is None, -(row.gebruikers or 0.0)))

    realms_total = _by_labels(answers["realms"], ())
    return Block(gemeten=True, rijen=list(rijen), totaal=realms_total.get(()))
