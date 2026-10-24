"""Wat voor soort lid er in ``users`` van een projectbestand staat."""

from __future__ import annotations

from enum import StrEnum


class LidSoort(StrEnum):
    """Een mens, of een service account.

    Een service account is geen aparte lijst naast ``users`` maar een lid in die lijst: hij
    draagt dezelfde rollen en kan dezelfde tokens hebben, maar er zit geen mens achter. Dat
    verschil telt bij elke lezer die iets met een MENS doet (Keycloak, uitnodiging, mail,
    OTP, de inloglijst); die lezen daarom
    :func:`opi.services.project_leden.human_members` en niet ``users`` zelf.
    """

    PERSOON = "person"
    SERVICE_ACCOUNT = "service-account"
