"""De localhost-grendel van de live groepen zelf, los van een echte afhankelijkheid.

d253407bd verplaatste ``assert_local()`` van de fixture ``live_connector`` naar ``drop`` en
``drop_by_prefix``: een toets zonder die fixture liep eerder ongemerkt een destructieve
``DROP DATABASE``/``DROP ROLE`` tegen het adres uit ``POSTGRES_LIVE_DSN``, zonder dat iets dat
adres controleerde. MinIO en Forgejo hadden dezelfde vorm, en daar stond de grendel ook
alleen in een fixture.

Dit bestand toetst de grendel (``LiveGroup.assert_local``) en dat elke weg die echte
toestand weghaalt hem aanroept voordat hij verbindt, zonder dat daar een echte server voor
nodig is.
"""

from __future__ import annotations

import re

import pytest
from tests.integration import forgejo_live, minio_live, postgres_live
from tests.integration.live import POSTGRES


class AdminConnectionCalled(Exception):
    """Signaal dat de grendel een verbindingspoging niet heeft tegengehouden."""


@pytest.mark.parametrize("address", ["localhost", "127.0.0.1", "localhost:5432"])
def test_assert_local_accepts_local_addresses(address: str) -> None:
    POSTGRES.assert_local(address)


def test_assert_local_accepts_an_ipv6_loopback_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression voor het dubbel parsen dat ``::1`` zou laten doorvallen op ``None``."""
    monkeypatch.setattr(postgres_live, "dsn", lambda: "postgresql://x:y@[::1]:5432/postgres")

    postgres_live.assert_local()


def test_assert_local_rejects_a_remote_address() -> None:
    with pytest.raises(pytest.fail.Exception, match=re.escape("rig-db-rw.rig-prd-operations")):
        POSTGRES.assert_local("rig-db-rw.rig-prd-operations")


def test_module_level_assert_local_rejects_a_remote_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    """De hele weg via ``postgres_live.assert_local()``, niet alleen ``POSTGRES.assert_local``."""
    monkeypatch.setattr(postgres_live, "dsn", lambda: "postgresql://x:y@rig-db-rw.rig-prd-operations:5432/postgres")

    with pytest.raises(pytest.fail.Exception, match=re.escape("rig-db-rw.rig-prd-operations")):
        postgres_live.assert_local()


async def test_drop_by_prefix_fails_before_connecting_to_a_remote_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(postgres_live, "dsn", lambda: "postgresql://x:y@rig-db-rw.rig-prd-operations:5432/postgres")

    async def would_connect() -> None:
        raise AdminConnectionCalled

    monkeypatch.setattr(postgres_live, "admin_connection", would_connect)

    with pytest.raises(pytest.fail.Exception, match=re.escape("rig-db-rw.rig-prd-operations")):
        await postgres_live.drop_by_prefix("zadlive")


async def test_drop_fails_before_connecting_to_a_remote_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(postgres_live, "dsn", lambda: "postgresql://x:y@rig-db-rw.rig-prd-operations:5432/postgres")

    async def would_connect() -> None:
        raise AdminConnectionCalled

    monkeypatch.setattr(postgres_live, "admin_connection", would_connect)

    with pytest.raises(pytest.fail.Exception, match=re.escape("rig-db-rw.rig-prd-operations")):
        await postgres_live.drop(databases=("zadlive_app",))


def test_minio_drop_by_prefix_fails_before_reaching_a_remote_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """``drop_by_prefix`` verwijdert buckets, gebruikers en policies: eerst de grendel."""
    monkeypatch.setattr(minio_live, "host", lambda: "minio.rig-prd-operations:9000")

    def would_reach() -> None:
        raise AdminConnectionCalled

    monkeypatch.setattr(minio_live, "admin_mc", would_reach)

    with pytest.raises(pytest.fail.Exception, match=re.escape("minio.rig-prd-operations")):
        minio_live.drop_by_prefix("zadlive")


def test_forgejo_api_fails_before_it_can_delete_a_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    """``fresh_repo`` en ``drop_repo`` verwijderen repositories; ``ForgejoApi`` komt er eerst.

    Met de inlog in de omgeving, want zonder die twee valt de constructie op een skip en zou
    het weghalen van de grendel geen rode toets geven maar een overgeslagen toets.
    """
    monkeypatch.setattr(forgejo_live, "base_url", lambda: "https://forgejo.rijksapps.nl")
    monkeypatch.setenv("FORGEJO_LIVE_USER", "zadlive")
    monkeypatch.setenv("FORGEJO_LIVE_PASSWORD", "example-Wachtwoord-1234")

    with pytest.raises(pytest.fail.Exception, match=re.escape("forgejo.rijksapps.nl")):
        forgejo_live.ForgejoApi()
