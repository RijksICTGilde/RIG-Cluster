"""Het handhavingspunt: wie is deze aanroeper, en mag hij deze handeling op dit project.

Hier komt elk bewijsmiddel binnen en wordt het tot EEN begrip vertaald, zodat er daarna nog
maar een beslissing is in plaats van een per soort:

- een token van een lid van het project. Het staat als hash bij dat lid in ``users`` van
  het projectbestand, dus het is er niet meer zodra het lid er niet meer is: een token is
  nooit meer dan de persoon of het service account waar het bij hoort;
- de oude projectsleutel uit ``config.api-key``, alleen nog voor een bestand dat de
  migratie naar schemaversie 2.12 niet heeft kunnen doorlopen omdat de sleutel niet open
  ging. Na die migratie IS de sleutel een token van het service account ``project-api``.

Wat een token mag volgt uit de ROL van het lid en staat niet in het token zelf. Daardoor
werkt een rolwijziging of een vertrek dezelfde seconde, zonder dat er iets ingetrokken
hoeft te worden, en is er geen tweede plek waar rechten staan die uit de pas kan lopen.
"""

from __future__ import annotations

import logging
import secrets
from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from opi.core.actor import Actor, project_api_key_actor, service_account_actor, user_actor
from opi.core.handelingen import LEESHANDELINGEN, Handeling
from opi.core.leden import LidSoort
from opi.services.project_leden import hash_van, is_verlopen, lid_met_naam
from opi.services.user_service import get_user_service

if TYPE_CHECKING:
    from opi.services.project_service import ProjectSummary, ProjectUser

logger = logging.getLogger(__name__)

#: De rollen die een project mogen WIJZIGEN. Dezelfde twee die
#: ``require_project_edit_access`` (``opi/web/project_edit_security.py``) in de interface
#: eist; een token hoort niet meer te openen dan zijn eigenaar daar al kan.
SCHRIJFROLLEN = frozenset({"admin", "owner"})

#: Alles wat een token op een project kan vragen. Een handeling die hier ontbreekt opent
#: voor niemand, dus een nieuwe handeling moet hier bewust bij.
ALLE_HANDELINGEN: frozenset[Handeling] = frozenset(Handeling)

#: Wat een rol opent. De koppeling tussen route en handeling staat in de catalogus
#: (``opi/core/handelingen.py``); dit is de tweede helft: van handeling naar rol. Een
#: schrijfrol krijgt alles, een leesrol exact de leeshandelingen, want dat is ook precies
#: het verschil dat de interface tussen die rollen maakt.
ROL_HANDELINGEN: dict[str, frozenset[Handeling]] = {
    "admin": ALLE_HANDELINGEN,
    "owner": ALLE_HANDELINGEN,
    "member": LEESHANDELINGEN,
    "developer": LEESHANDELINGEN,
}

#: Handelingen die een GEVERIFIEERDE MENS vragen, ONGEACHT het doelwit, en dus nooit een
#: service account. Vandaag is dat het uitgeven en intrekken van tokens: wie een token kan
#: maken kan zijn eigen bewijsmiddel vermenigvuldigen, en dat hoort geen pijplijn te kunnen.
#:
#: Dit is NIET de step-upcatalogus van RC-332 (``StepUpActie`` in ``opi/core/step_up.py``),
#: en met opzet niet, want de twee antwoorden op een andere vraag:
#:
#: * hier: mag deze SOORT aanroeper dit ooit. Onvoorwaardelijk, en een service account valt
#:   er altijd op af, met of zonder code;
#: * RC-332: vraagt DEZE aanroep een verse tweede factor. Voorwaardelijk, per doelwit (een
#:   deployment op productie wel, dezelfde op acceptatie niet), en per route verklaard met
#:   ``@requires_step_up``.
#:
#: Gelijktrekken zou de verkeerde kant op werken: ``PROJECT_VERWIJDEREN`` hier zetten sluit
#: een pijplijn ook uit van een project zonder productie, en dat is precies de voorwaarde
#: die RC-332 weegt. Andersom zou ``TOKEN_BEHEREN`` een step-upactie moeten worden, maar die
#: handeling staat alleen op bearerroutes en is met geen token te bereiken. Welke handeling
#: bij welke step-upactie hoort legt ``tests/test_handelingen.py`` vast, zodat de twee
#: catalogi niet stil uit elkaar kunnen lopen.
#:
#: Een service account op een step-up-route loopt dus niet hier, maar in
#: :func:`opi.api.otp_step_up.verify_api_step_up` af, met dezelfde reden: het token hoort
#: bij geen gebruiker, dus er is geen factor om tegen te toetsen.
MENS_VERPLICHT: frozenset[Handeling] = frozenset({Handeling.TOKEN_BEHEREN})

#: De ``cause`` waarmee een service account op zo'n handeling afketst. Een eigen reden en
#: niet "te weinig rechten": de rol klopt, de SOORT aanroeper niet, en dat is iets anders
#: om op te lossen.
TOKENSOORT_NIET_TOEGESTAAN = "tokensoort-niet-toegestaan"


class PrincipaalSoort(StrEnum):
    """Welk bewijsmiddel de aanroeper gebruikte."""

    PROJECTSLEUTEL = "projectsleutel"
    PERSOON = "persoon"
    SERVICE_ACCOUNT = "service-account"


class NietHerkend(Exception):
    """Het bewijsmiddel is onbekend of verlopen (401)."""


class ToegangGeweigerd(Exception):
    """Het bewijsmiddel is geldig maar mag deze handeling hier niet (403)."""

    def __init__(self, bericht: str, *, cause: str = "te-weinig-rechten") -> None:
        super().__init__(bericht)
        self.cause = cause


@dataclass(frozen=True)
class Principaal:
    """Een herkende aanroeper, los van hoe hij zich legitimeerde."""

    soort: PrincipaalSoort
    naam: str
    project: str
    handelingen: frozenset[Handeling]
    actor: Actor


# --- meting van het afbouwpad -----------------------------------------------
#
# Zonder deze telling is "de projectsleutel verdwijnt zodra niemand hem meer gebruikt" een
# wens. De tabel per (project, soort) is wat ``opi/core/metrics.py`` als teller uitdeelt,
# zodat Prometheus de trend bewaart.
_gebruik: dict[tuple[str, str], int] = defaultdict(int)


def tel_gebruik(project: str, soort: PrincipaalSoort) -> None:
    """Tel een geslaagde legitimatie, per project en per soort bewijsmiddel."""
    _gebruik[(project, soort.value)] += 1


def gebruik_per_project() -> dict[tuple[str, str], int]:
    """De telling tot nu toe. Gelezen door de Prometheus-collector."""
    return dict(_gebruik)


def reset_gebruik() -> None:
    """Zet de telling terug. Alleen voor tests."""
    _gebruik.clear()


def handelingen_van_rol(rol: str) -> frozenset[Handeling]:
    """Wat deze rol op een project opent. Een onbekende rol opent niets."""
    return ROL_HANDELINGEN.get(rol, frozenset())


def _token_eigenaar(project: ProjectSummary, presented: str) -> ProjectUser | None:
    """Het lid wiens token hier gepresenteerd wordt, of None.

    Op bytes vergelijken en over ALLE tokens doorlopen, zodat de duur van de lus niets over
    de treffer zegt.
    """
    gepresenteerd = hash_van(presented).encode()
    gevonden: ProjectUser | None = None
    for lid in project.users or []:
        for token in lid.tokens:
            if secrets.compare_digest(token.hash.encode(), gepresenteerd):
                if is_verlopen(token.expires):
                    logger.warning("Token '%s' van '%s' is verlopen", token.name, lid.principaal)
                    continue
                gevonden = lid
    return gevonden


async def herken_principaal(presented: str, project: ProjectSummary) -> Principaal:
    """Vertaal een ``X-API-Key`` naar een principaal van DIT project.

    Het project komt als object binnen en wordt hier niet opnieuw opgehaald: de ingang die
    de sleutel tegen een project legitimeert heeft het al gelezen, en een tweede lezing zou
    dezelfde beslissing op een ander moment baseren.

    Een token van een ANDER project valt hier als niet herkend en niet als geweigerd: of
    een project bestaat is zelf al iets dat een onbevoegde aanroeper niet hoort te kunnen
    aflezen, en een verschil tussen die twee antwoorden maakt projectnamen aftastbaar.

    Raises:
        NietHerkend: Als het bewijsmiddel onbekend of verlopen is.
    """
    lid = _token_eigenaar(project, presented)
    if lid is not None:
        if lid.type is LidSoort.SERVICE_ACCOUNT:
            naam = lid.name or ""
            return Principaal(
                soort=PrincipaalSoort.SERVICE_ACCOUNT,
                naam=naam,
                project=project.name,
                handelingen=handelingen_van_rol(lid.role),
                actor=service_account_actor(project.name, naam),
            )
        # Een token van een mens is niet meer dan die mens: wie geen platformgebruiker meer
        # is, is hier ook niet meer herkend. Zonder deze lezing overleeft het token een
        # offboarding in het gebruikersbeheer, dat geen projectbestand raakt.
        email = lid.email or ""
        if not get_user_service().is_email_allowed(email):
            raise NietHerkend(f"'{email}' heeft geen toegang tot het platform")
        return Principaal(
            soort=PrincipaalSoort.PERSOON,
            naam=email,
            project=project.name,
            handelingen=handelingen_van_rol(lid.role),
            actor=user_actor(email),
        )

    # De oude projectsleutel. Hij staat alleen nog in een bestand waarvan de migratie naar
    # 2.12 hem niet open kreeg; na die migratie is hij een token van ``project-api``.
    if project.api_key and secrets.compare_digest(project.api_key.encode(), presented.encode()):
        return Principaal(
            soort=PrincipaalSoort.PROJECTSLEUTEL,
            naam=project.name,
            project=project.name,
            handelingen=ALLE_HANDELINGEN - MENS_VERPLICHT,
            actor=project_api_key_actor(project.name),
        )

    raise NietHerkend("bewijsmiddel is onbekend of verlopen")


def hoort_bij_project(principaal: Principaal, project: ProjectSummary) -> bool:
    """Of dit bewijsmiddel bij dit project hoort, zonder al een handeling te beslissen.

    Voor de voorcontrole van de image-push. :func:`herken_principaal` legt het bewijsmiddel
    al tegen dit project, dus hier blijft over of het lid er nog staat.
    """
    if principaal.soort is PrincipaalSoort.PROJECTSLEUTEL:
        return principaal.project == project.name
    return lid_met_naam(project, principaal.naam) is not None


def beslis(principaal: Principaal, *, project: ProjectSummary, handeling: Handeling) -> None:
    """Laat deze handeling op dit project toe, of weiger hem.

    Dat het bewijsmiddel bij DIT project hoort is hier al vastgesteld, in
    :func:`herken_principaal`; wat hier nog valt is een geldig bewijsmiddel dat te weinig
    mag, of de verkeerde SOORT aanroeper voor deze handeling.

    De rol wordt hier OPNIEUW gelezen en niet uit de principaal overgenomen: tussen het
    herkennen en het beslissen kan het projectbestand zijn veranderd, en dan telt wat er nu
    staat.

    Raises:
        ToegangGeweigerd: Als het bewijsmiddel deze handeling hier niet opent.
    """
    if principaal.soort is PrincipaalSoort.SERVICE_ACCOUNT and handeling in MENS_VERPLICHT:
        raise ToegangGeweigerd(
            f"'{handeling.value}' vraagt een geverifieerde persoon; een service account heeft geen tweede factor",
            cause=TOKENSOORT_NIET_TOEGESTAAN,
        )

    if principaal.soort is PrincipaalSoort.PROJECTSLEUTEL:
        if handeling not in principaal.handelingen:
            raise ToegangGeweigerd(f"de projectsleutel opent '{handeling.value}' niet")
        return

    lid = lid_met_naam(project, principaal.naam)
    if lid is None:
        raise ToegangGeweigerd(f"'{principaal.naam}' is geen lid van project '{project.name}'")

    toegestaan = handelingen_van_rol(lid.role)
    if handeling not in toegestaan:
        raise ToegangGeweigerd(f"rol '{lid.role}' opent '{handeling.value}' niet in dit project")
