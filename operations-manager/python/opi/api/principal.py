"""Het handhavingspunt van de API: wie is deze aanroeper, en mag hij deze action hier.

Hier komt elk bewijsmiddel binnen en wordt het tot EEN begrip vertaald, zodat er daarna nog
maar een beslissing is in plaats van een per soort:

- een token van een lid van het project. Het staat als hash bij dat lid in ``users`` van
  het projectbestand, dus het is er niet meer zodra het lid er niet meer is: een token is
  nooit meer dan de persoon of het service account waar het bij hoort;
- de oude projectsleutel uit ``config.api-key``, alleen nog voor een bestand dat de
  migratie naar schemaversie 2.12 niet heeft kunnen doorlopen omdat de sleutel niet open
  ging. Na die migratie IS de sleutel een token van het service account ``project-api``.

Wat een token mag volgt uit de ROLLEN van het lid en staat niet in het token zelf. Daardoor
werkt een rolwijziging of een vertrek dezelfde seconde, zonder dat er iets ingetrokken
hoeft te worden, en is er geen tweede plek waar rechten staan die uit de pas kan lopen.
De beslissing zelf valt in :func:`opi.core.authz.engine.decide`, dezelfde functie die het
portaal gebruikt.
"""

from __future__ import annotations

import logging
import secrets
from collections import defaultdict
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from opi.core.actor import project_api_key_actor, service_account_actor, user_actor
from opi.core.authz.engine import (
    Cause,
    Decision,
    Principal,
    PrincipalKind,
    Resource,
    decide,
    record_decision,
)
from opi.core.leden import LidSoort
from opi.services.project_leden import hash_van, is_verlopen, lid_met_naam
from opi.services.project_roles import PLATFORM_ADMIN_ROLES, build_resource, member_roles
from opi.services.user_service import get_user_service

if TYPE_CHECKING:
    from opi.core.authz.actions import Action
    from opi.services.project_service import ProjectSummary, ProjectUser

logger = logging.getLogger(__name__)

#: De ``cause`` waarmee een aanroeper afketst die de juiste rol heeft maar de verkeerde
#: soort is. Een eigen reden en niet "te weinig rechten": de rol klopt, de SOORT niet, en
#: dat is iets anders om op te lossen.
PRINCIPAL_KIND_NOT_ALLOWED = "principal-kind-not-allowed"


class NotRecognised(Exception):
    """Het bewijsmiddel is onbekend of verlopen (401)."""


class AccessDenied(Exception):
    """Het bewijsmiddel is geldig maar mag deze action hier niet (403)."""

    def __init__(self, bericht: str, *, cause: str = "insufficient-role") -> None:
        super().__init__(bericht)
        self.cause = cause


# --- meting van het afbouwpad -----------------------------------------------
#
# Zonder deze telling is "de projectsleutel verdwijnt zodra niemand hem meer gebruikt" een
# wens. De tabel per (project, soort) is wat ``opi/core/metrics.py`` als teller uitdeelt,
# zodat Prometheus de trend bewaart.
_gebruik: dict[tuple[str, str], int] = defaultdict(int)


def tel_gebruik(project: str, soort: PrincipalKind) -> None:
    """Tel een geslaagde legitimatie, per project en per soort bewijsmiddel."""
    _gebruik[(project, soort.value)] += 1


def gebruik_per_project() -> dict[tuple[str, str], int]:
    """De telling tot nu toe. Gelezen door de Prometheus-collector."""
    return dict(_gebruik)


def reset_gebruik() -> None:
    """Zet de telling terug. Alleen voor tests."""
    _gebruik.clear()


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


async def recognise_principal(presented: str, project: ProjectSummary) -> Principal:
    """Vertaal een ``X-API-Key`` naar een principal van DIT project.

    Het project komt als object binnen en wordt hier niet opnieuw opgehaald: de ingang die
    de sleutel tegen een project legitimeert heeft het al gelezen, en een tweede lezing zou
    dezelfde beslissing op een ander moment baseren.

    Een token van een ANDER project valt hier als niet herkend en niet als geweigerd: of
    een project bestaat is zelf al iets dat een onbevoegde aanroeper niet hoort te kunnen
    aflezen, en een verschil tussen die twee antwoorden maakt projectnamen aftastbaar.

    Raises:
        NotRecognised: Als het bewijsmiddel onbekend of verlopen is.
    """
    lid = _token_eigenaar(project, presented)
    if lid is not None:
        if lid.type is LidSoort.SERVICE_ACCOUNT:
            naam = lid.name or ""
            return Principal(
                kind=PrincipalKind.SERVICE_ACCOUNT,
                name=naam,
                project=project.name,
                roles=member_roles(lid),
                actor=service_account_actor(project.name, naam),
            )
        # Een token van een mens is niet meer dan die mens: wie geen platformgebruiker meer
        # is, is hier ook niet meer herkend. Zonder deze lezing overleeft het token een
        # offboarding in het gebruikersbeheer, dat geen projectbestand raakt.
        email = lid.email or ""
        if not get_user_service().is_email_allowed(email):
            raise NotRecognised(f"'{email}' heeft geen toegang tot het platform")
        return Principal(
            kind=PrincipalKind.HUMAN,
            name=email,
            project=project.name,
            roles=member_roles(lid),
            actor=user_actor(email),
        )

    # De oude projectsleutel. Hij staat alleen nog in een bestand waarvan de migratie naar
    # 2.12 hem niet open kreeg; na die migratie is hij een token van ``project-api``. Hij
    # draagt de rollen van een beheerder; wat een mens vraagt valt voor hem af op de SOORT.
    if project.api_key and secrets.compare_digest(project.api_key.encode(), presented.encode()):
        return Principal(
            kind=PrincipalKind.PROJECT_KEY,
            name=project.name,
            project=project.name,
            roles=PLATFORM_ADMIN_ROLES,
            actor=project_api_key_actor(project.name),
        )

    raise NotRecognised("bewijsmiddel is onbekend of verlopen")


def hoort_bij_project(principal: Principal, project: ProjectSummary) -> bool:
    """Of dit bewijsmiddel bij dit project hoort, zonder al een action te beslissen.

    Voor de voorcontrole van de image-push. :func:`recognise_principal` legt het bewijsmiddel
    al tegen dit project, dus hier blijft over of het lid er nog staat.
    """
    if principal.kind is PrincipalKind.PROJECT_KEY:
        return principal.project == project.name
    return lid_met_naam(project, principal.name) is not None


def beslis(
    principal: Principal,
    *,
    project: ProjectSummary,
    handeling: Action,
    resource: Resource | None = None,
    step_up_fresh: bool = True,
    request: Any = None,
) -> Decision:
    """Laat deze action op dit project toe, of weiger hem.

    Dat het bewijsmiddel bij DIT project hoort is hier al vastgesteld, in
    :func:`recognise_principal`; wat hier nog valt is een geldig bewijsmiddel dat te weinig
    mag, of de verkeerde SOORT aanroeper voor deze action.

    De rollen worden hier OPNIEUW gelezen en niet uit de principal overgenomen: tussen het
    herkennen en het beslissen kan het projectbestand zijn veranderd, en dan telt wat er nu
    staat.

    Een ontbrekende step-up is hier geen weigering: die wordt per route opgelegd door
    ``@requires_step_up``, dat zijn eigen antwoord met de tweede factor geeft.

    Raises:
        AccessDenied: Als het bewijsmiddel deze action hier niet opent.
    """
    if principal.kind is PrincipalKind.PROJECT_KEY:
        actueel = principal
    else:
        lid = lid_met_naam(project, principal.name)
        if lid is None:
            raise AccessDenied(f"'{principal.name}' is geen lid van project '{project.name}'")
        actueel = replace(principal, roles=member_roles(lid))

    doel = (
        resource
        if resource is not None
        else build_resource(project, project_name=project.name, path=f"project/{project.name}")
    )
    beslissing = decide(actueel, handeling, doel, step_up_fresh=step_up_fresh)
    if request is not None:
        record_decision(request, action=handeling, resource=doel, decision=beslissing, step_up_fresh=step_up_fresh)
    if beslissing.allowed or beslissing.cause is Cause.STEP_UP_REQUIRED or beslissing.step_up_would_help:
        return beslissing
    if beslissing.cause is Cause.PRINCIPAL_KIND_NOT_ALLOWED:
        raise AccessDenied(
            f"'{handeling.value}' vraagt een geverifieerde persoon; een service account heeft geen tweede factor",
            cause=PRINCIPAL_KIND_NOT_ALLOWED,
        )
    raise AccessDenied(f"de rollen van '{principal.name}' openen '{handeling.value}' niet in dit project")
