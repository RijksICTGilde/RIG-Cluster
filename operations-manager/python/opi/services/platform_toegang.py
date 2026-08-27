"""De gegevens achter de beheerpagina Toegang: waar de platformdiensten staan en hoe je
erin komt.

RENDEREN, NIET OPSLAAN. Elke waarde hier wordt op het moment van opvragen uit het cluster
gelezen. Het alternatief dat op tafel lag was alles een keer in een geaggregeerd geheim of
een JSON-blok zetten, en dat heeft drie nadelen die dit pad niet heeft: er ontstaat een
tweede kopie die kan verouderen, er ontstaat een object dat het hele platform waard is om
te stelen, en een rotatie moet iemand met de hand doorvoeren. De wachtwoorden staan toch al
op het cluster; het waardevolle is de AGGREGATIE, en aggregeren kun je bij het tonen.

Deze pagina is voor GEMAK zolang het cluster leeft. Voor de situatie dat het cluster er
niet meer is helpt hij per definitie niet; daar is een export voor, en die is bewust
geparkeerd.

WELKE DIENSTEN. Alleen waar een mens zelf inlogt: Keycloak, Forgejo en ArgoCD. Nadrukkelijk
niet de koppelingen tussen componenten onderling (de databaserollen, Redis, het
metrics-token, de relay-admin, chisel). Die zijn de meerderheid, en als je ze kwijtraakt
genereer je ze opnieuw; ze in een overzicht zetten maakt de lijst lang en daarmee de vijf
regels die er wel toe doen onvindbaar. pgAdmin staat op elk cluster uitgecommentarieerd en
staat er daarom niet in.

EEN CLUSTER ZONDER EEN DIENST TOONT GEEN DODE REGEL. Ontbreekt het Secret, dan bestaat de
dienst hier niet. Dezelfde vorm als ``has_mail_relay``: de omgeving beantwoordt de
beschikbaarheidsvraag en deze module noemt geen enkel cluster.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from opi.connectors.kubectl import create_kubectl_connector
from opi.core.cluster_config import get_ingress_postfix, get_namespace
from opi.core.config import settings
from opi.utils.totp import totp_now

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DienstBron:
    """Waar een dienst zijn gegevens vandaan haalt.

    ``host_prefix`` doet twee dingen: hij zoekt de Ingress op en hij vormt de terugval.
    Let op dat hij niet af te leiden is uit de naam van de dienst; ArgoCD hangt op dit
    platform onder ``argo`` en niet onder ``argocd``. Gemeten, niet geraden.
    """

    naam: str
    icoon: str
    secret_naam: str
    wachtwoord_veld: str
    host_prefix: str
    gebruiker_veld: str | None = None
    vaste_gebruiker: str | None = None


# De volgorde is de volgorde op het scherm, en die is niet willekeurig: Keycloak eerst,
# want zonder inloggen kom je nergens; daarna waar de code staat; daarna wat hem uitrolt.
DIENSTEN: tuple[DienstBron, ...] = (
    DienstBron(
        naam="Keycloak",
        icoon="shield-check-mark",
        secret_naam="keycloak-admin-credentials",
        gebruiker_veld="KEYCLOAK_ADMIN",
        wachtwoord_veld="KEYCLOAK_ADMIN_PASSWORD",
        host_prefix="keycloak",
    ),
    DienstBron(
        naam="Forgejo",
        icoon="code",
        secret_naam="forgejo-admin",
        gebruiker_veld="username",
        wachtwoord_veld="password",
        host_prefix="forgejo",
    ),
    DienstBron(
        naam="ArgoCD",
        # "sync" en niet "synchroniseren": NLDD kent de Nederlandse naam niet en rendert
        # hem leeg, zonder foutmelding. Gecontroleerd tegen opi/web/nldd_iconen.py.
        icoon="sync",
        # Niet uit onze eigen blauwdruk: de argocd-operator maakt dit wachtwoord zelf aan
        # en zet het in <cr-naam>-cluster, in platte tekst. De blauwdruk
        # bootstrap/rig-system/kustomize/secrets/templates/argocd-admin-secret.yaml maakt
        # een BCRYPT-HASH, en daar valt niets uit terug te lezen; die is een restant van de
        # opzet van voor de operator. OPI leest het wachtwoord al uit dezelfde plek.
        secret_naam="argocd-cluster",
        vaste_gebruiker="admin",
        wachtwoord_veld="admin.password",
        host_prefix="argo",
    ),
)


@dataclass
class ToegangRegel:
    """Een dienst zoals hij op het scherm komt."""

    naam: str
    icoon: str
    url: str
    gebruiker: str
    wachtwoord: str
    # Alleen gevuld als de Ingress en de clusterconfiguratie het oneens zijn. Dat is geen
    # schoonheidsfoutje: een dienst die niet onder de postfix van zijn eigen cluster hangt
    # betekent een halve domeinmigratie, en die vind je liever hier dan in een SERVFAIL.
    waarschuwing: str = ""
    extra_velden: list[tuple[str, str]] = field(default_factory=list)


async def _hosts_uit_ingresses(namespace: str) -> dict[str, str]:
    """De hosts van elke Ingress in de namespace, op hun eerste label gezet.

    De Ingress is de waarheid: hij volgt een domeinwijziging vanzelf, en een gedeclareerde
    URL kan er ongemerkt naast gaan liggen.
    """
    connector = create_kubectl_connector()
    ingresses: list[dict[str, Any]] = await connector.get_resources_by_label("ingress", namespace, "")

    hosts: dict[str, str] = {}
    for ingress in ingresses:
        for regel in (ingress.get("spec") or {}).get("rules") or []:
            host = regel.get("host")
            if not host:
                continue
            hosts.setdefault(host.split(".", 1)[0], host)
    return hosts


async def _lees_dienst(bron: DienstBron, namespace: str, hosts: dict[str, str], postfix: str) -> ToegangRegel | None:
    connector = create_kubectl_connector()
    data = await connector.get_secret(bron.secret_naam, namespace)
    if not data:
        logger.debug(f"Toegang: {bron.naam} overgeslagen, secret {bron.secret_naam} bestaat niet in {namespace}")
        return None

    wachtwoord = data.get(bron.wachtwoord_veld)
    if not wachtwoord:
        logger.warning(
            f"Toegang: secret {bron.secret_naam} bestaat wel maar heeft geen veld {bron.wachtwoord_veld}; "
            f"{bron.naam} wordt zonder wachtwoord getoond"
        )

    gebruiker = bron.vaste_gebruiker or data.get(bron.gebruiker_veld or "", "") or ""

    gedeclareerd = f"https://{bron.host_prefix}{postfix}"
    gevonden = hosts.get(bron.host_prefix)
    waarschuwing = ""
    if gevonden:
        url = f"https://{gevonden}"
        if url != gedeclareerd:
            waarschuwing = (
                f"De Ingress wijst naar {gevonden}, terwijl de clusterconfiguratie "
                f"{bron.host_prefix}{postfix} verwacht. Een van de twee loopt achter."
            )
    else:
        url = gedeclareerd
        waarschuwing = "Er is geen Ingress voor deze dienst gevonden; dit adres komt uit de clusterconfiguratie."

    regel = ToegangRegel(
        naam=bron.naam,
        icoon=bron.icoon,
        url=url,
        gebruiker=gebruiker,
        wachtwoord=wachtwoord or "",
        waarschuwing=waarschuwing,
    )

    # De OTP-beheerder hoort bij Keycloak en zit in hetzelfde geheim, dus hij kost geen
    # extra aanroep. Wat er komt te staan is de CODE VAN DIT MOMENT en niet de seed: een
    # seed geeft voor altijd codes, deze code vergaat binnen dertig seconden. Zelfde
    # afweging als op het tabblad Services info van een project.
    if data.get("KEYCLOAK_OTP_ADMIN_USERNAME"):
        regel.extra_velden.append(("OTP-beheerder", data["KEYCLOAK_OTP_ADMIN_USERNAME"]))
        if data.get("KEYCLOAK_OTP_ADMIN_PASSWORD"):
            regel.extra_velden.append(("Wachtwoord OTP-beheerder", data["KEYCLOAK_OTP_ADMIN_PASSWORD"]))
        if data.get("KEYCLOAK_OTP_ADMIN_TOTP_SECRET"):
            try:
                code, _ = totp_now(data["KEYCLOAK_OTP_ADMIN_TOTP_SECRET"])
                regel.extra_velden.append(("OTP-code (nu)", code))
            except Exception:
                logger.warning("Toegang: OTP-code kon niet worden afgeleid", exc_info=True)

    return regel


async def haal_toegang() -> list[ToegangRegel]:
    """Alle diensten waar een mens op dit cluster zelf op inlogt.

    De aanroepen gaan NAAST elkaar: elke ``kubectl`` is een eigen proces, en achter elkaar
    wachten kost hier vier procesforks op een rij voor een pagina die niets anders doet.
    """
    cluster = settings.CLUSTER_MANAGER
    namespace = get_namespace(cluster)
    postfix = get_ingress_postfix(cluster)

    hosts = await _hosts_uit_ingresses(namespace)

    regels = await asyncio.gather(
        *(_lees_dienst(bron, namespace, hosts, postfix) for bron in DIENSTEN),
        return_exceptions=True,
    )

    resultaat: list[ToegangRegel] = []
    for bron, regel in zip(DIENSTEN, regels, strict=True):
        if isinstance(regel, BaseException):
            # Een dienst die niet gelezen kan worden mag de andere niet meenemen. De
            # beheerder die hier komt heeft meestal aan een van de drie genoeg.
            logger.error(f"Toegang: {bron.naam} kon niet worden gelezen: {regel}")
            continue
        if regel is not None:
            resultaat.append(regel)
    return resultaat
