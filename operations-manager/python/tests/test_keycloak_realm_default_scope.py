"""Een 409 op een client scope die al realm-default is, is de gewenste eindtoestand.

Bevinding 6 van de releasecontrole van oktober 2026: elke herhaalde bootstrap tegen een
Keycloak waar ``custom_attributes_passthrough`` al realm-default stond, zette twee
ERROR-regels in het opstartlog van OPI, terwijl het opstarten er zonder onderbreking
langs liep. Een ERROR die bij normaal gedrag hoort maakt het log onleesbaar voor de
fouten die wel iets betekenen.
"""

import logging
from unittest.mock import MagicMock

import pytest
from keycloak.exceptions import KeycloakPostError
from opi.connectors.keycloak import KeycloakConnector


def _connector(side_effect) -> KeycloakConnector:
    connector = KeycloakConnector.__new__(KeycloakConnector)
    connector.admin = MagicMock()
    connector.admin.add_default_default_client_scope = MagicMock(side_effect=side_effect)
    return connector


@pytest.mark.asyncio
async def test_een_409_is_geen_fout_maar_de_eindtoestand(caplog: pytest.LogCaptureFixture) -> None:
    connector = _connector(KeycloakPostError(error_message=b'{"error":"unknown_error"}', response_code=409))

    with caplog.at_level(logging.INFO, logger="opi.connectors.keycloak"):
        assert await connector.assign_client_scope_as_realm_default("rig-platform", "scope-1") is True

    regels = [r for r in caplog.records if "already realm-level default" in r.message]
    assert regels, f"geen INFO-regel over de bestaande realm-default: {[r.message for r in caplog.records]}"
    assert [r.levelno for r in regels] == [logging.INFO]
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], (
        f"een 409 hoort geen ERROR te geven: {[r.message for r in caplog.records if r.levelno >= logging.ERROR]}"
    )
    # En de realm staat terug op master, net als op het geslaagde pad.
    connector.admin.change_current_realm.assert_called_with("master")


@pytest.mark.asyncio
async def test_een_andere_fout_blijft_een_error(caplog: pytest.LogCaptureFixture) -> None:
    connector = _connector(KeycloakPostError(error_message=b'{"error":"boom"}', response_code=500))

    with caplog.at_level(logging.INFO, logger="opi.connectors.keycloak"):
        assert await connector.assign_client_scope_as_realm_default("rig-platform", "scope-1") is False

    fouten = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(fouten) == 1, f"verwachtte een ERROR-regel, kreeg: {[(r.levelname, r.message) for r in caplog.records]}"
    assert "Failed to assign client scope as realm default" in fouten[0].message
    connector.admin.change_current_realm.assert_called_with("master")


@pytest.mark.asyncio
async def test_een_409_in_de_melding_van_een_andere_fout_blijft_een_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """De 409 komt van de statuscode, niet van een substring in het antwoordlichaam."""
    connector = _connector(KeycloakPostError(error_message=b'{"error":"409 upstream kapot"}', response_code=500))

    with caplog.at_level(logging.INFO, logger="opi.connectors.keycloak"):
        assert await connector.assign_client_scope_as_realm_default("rig-platform", "scope-1") is False

    fouten = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(fouten) == 1, f"verwachtte een ERROR-regel, kreeg: {[(r.levelname, r.message) for r in caplog.records]}"


@pytest.mark.asyncio
async def test_het_geslaagde_pad_meldt_niets_op_error(caplog: pytest.LogCaptureFixture) -> None:
    connector = _connector(None)

    with caplog.at_level(logging.INFO, logger="opi.connectors.keycloak"):
        assert await connector.assign_client_scope_as_realm_default("rig-platform", "scope-1") is True

    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    connector.admin.add_default_default_client_scope.assert_called_once_with(scope_id="scope-1")
