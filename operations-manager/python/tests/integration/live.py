"""Gedeelde hulp voor de groepen die tegen een echte afhankelijkheid meten.

Elke afhankelijkheid heeft zijn eigen groep (``keycloak_live``, ``postgres_live``,
``minio_live``, ``forgejo_live``, ``kubernetes_live``, ``argocd_live``) en zijn eigen task
die de gepinde image start en de adressen in de omgeving zet. Wat die groepen delen staat
hier: het lezen van die omgeving, en de grendel die weigert tegen iets anders dan deze
machine te draaien.

Zonder de omgeving slaat een groep zichzelf over. Met de vereist-variabele van de groep
gezet is een ontbrekende waarde een FOUT: dat is wat de CI zet, want daar betekent
"overgeslagen" dat de job groen is zonder dat er iets gemeten is.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse

import pytest

#: Waar een groep tegen mag meten. Elke groep maakt en verwijdert echte toestand, dus op een
#: gedeelde of productie-instantie zou hij destructief zijn.
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


@dataclass(frozen=True)
class LiveGroup:
    """Een groep toetsen tegen een echte afhankelijkheid, en de omgeving die hem aanwijst."""

    name: str
    #: De marker van de groep. Niet uit ``name`` te leiden: die is prose ("PostgreSQL").
    marker: str
    required_variable: str

    def omgeving(self, naam: str) -> str:
        """De waarde van een omgevingsvariabele van de meetopstelling, of een skip."""
        waarde = os.environ.get(naam)
        if not waarde:
            melding = f"{naam} niet gezet, geen {self.name} om tegen te meten"
            if os.environ.get(self.required_variable):
                pytest.fail(melding)
            pytest.skip(melding)
        return waarde

    def assert_local(self, address: str) -> None:
        """Weiger tegen iets anders dan een wegwerp-instantie op deze machine te draaien."""
        host = _hostname(address)
        if host not in LOCAL_HOSTS:
            pytest.fail(
                f"De groep {self.marker} wijst naar '{host}'. Hij maakt en verwijdert echte "
                "toestand en draait alleen tegen een wegwerp-instantie op localhost."
            )


def _hostname(address: str) -> str | None:
    if "://" in address:
        return urlparse(address).hostname
    return urlparse(f"//{address}").hostname


KEYCLOAK = LiveGroup(name="Keycloak", marker="keycloak_live", required_variable="KEYCLOAK_LIVE_REQUIRED")
POSTGRES = LiveGroup(name="PostgreSQL", marker="postgres_live", required_variable="POSTGRES_LIVE_REQUIRED")
MINIO = LiveGroup(name="MinIO", marker="minio_live", required_variable="MINIO_LIVE_REQUIRED")
FORGEJO = LiveGroup(name="Forgejo", marker="forgejo_live", required_variable="FORGEJO_LIVE_REQUIRED")
KUBERNETES = LiveGroup(name="Kubernetes", marker="kubernetes_live", required_variable="KUBERNETES_LIVE_REQUIRED")
ARGOCD = LiveGroup(name="ArgoCD", marker="argocd_live", required_variable="ARGOCD_LIVE_REQUIRED")
