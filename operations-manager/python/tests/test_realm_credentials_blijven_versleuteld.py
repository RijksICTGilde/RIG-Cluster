"""Realm-credentials bestaan alleen versleuteld in het projectbestand (RC-410).

``_setup_project_keycloak_realm`` gaf aan het eind een dict terug met het PLATTE
realm-adminwachtwoord, de platte TOTP-seed en de otpauth-URI. Geen enkele aanroeper las
die waarden, terwijl ze al AGE-versleuteld in het projectbestand stonden. Een return-waarde
als deze is wel precies wat later in een taakresultaat belandt, en dat resultaat wordt bij
voltooien niet geredigeerd (``opi/core/async_task_service.py``).

Wat hier gemeten wordt: de functie geeft niets terug, het projectbestand draagt de
credentials alleen versleuteld, en geen taakhandler zet zo'n waarde in zijn resultaat.
"""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pyrage
import pytest
from opi.manager.keycloak_manager import KeycloakManager
from opi.services.project import Project
from opi.utils.age import decrypt_password_smart

#: De sleutels die nooit in een return-waarde horen te staan.
GEHEIME_SLEUTELS = frozenset({"password", "totp_secret", "totp_otpauth_uri"})

HANDLERS = sorted((Path(__file__).resolve().parents[1] / "opi" / "core").glob("task_handlers*.py"))


@pytest.mark.asyncio
async def test_de_realmaanmaak_geeft_niets_terug_en_versleutelt_alles() -> None:
    """De credentials staan alleen in het projectbestand, en daar alleen versleuteld."""
    identity = pyrage.x25519.Identity.generate()
    project_data = {"config": {"age-public-key": str(identity.to_public())}}

    project_manager = MagicMock()
    project_manager.get_contents = AsyncMock(return_value=project_data)
    project_manager.save_and_commit_project = AsyncMock()
    manager = KeycloakManager(project_manager)

    keycloak = AsyncMock()
    keycloak.get_user_by_username = AsyncMock(return_value=None)
    keycloak.create_user = AsyncMock(return_value={"id": "nieuwe-gebruiker"})
    keycloak.assign_realm_admin_from_master = AsyncMock()

    with (
        patch("opi.manager.keycloak_manager.create_keycloak_connector", return_value=keycloak),
        patch("opi.manager.keycloak_manager.KeycloakYamlHandler") as yaml_handler,
        patch("opi.manager.keycloak_manager.get_keycloak_support_http", return_value=False),
        patch("opi.manager.keycloak_manager.generate_secure_password", return_value="HetPlatteWachtwoord"),
    ):
        yaml_handler.return_value.execute_config = AsyncMock()

        uitkomst = await manager._setup_project_keycloak_realm(
            project_name="regel-k4c",
            cluster="odcn-production",
            keycloak_url="https://keycloak.example",
            config={"template": "sso-support"},
        )

    assert uitkomst is None

    bewaard = project_manager.save_and_commit_project.await_args.args[0]
    entry = Project(bewaard).get("services/keycloak/config/realms")[0]
    assert "HetPlatteWachtwoord" not in str(bewaard)
    assert await decrypt_password_smart(entry["password"], str(identity)) == "HetPlatteWachtwoord"


def _geheime_sleutels_in_returns(bestand: Path) -> list[tuple[int, str]]:
    """De regels waar een ``return`` een dict met een geheime sleutel teruggeeft."""
    gevonden: list[tuple[int, str]] = []
    for knoop in ast.walk(ast.parse(bestand.read_text())):
        if not isinstance(knoop, ast.Return) or not isinstance(knoop.value, ast.Dict):
            continue
        gevonden.extend(
            (knoop.lineno, str(sleutel.value))
            for sleutel in knoop.value.keys
            if isinstance(sleutel, ast.Constant) and sleutel.value in GEHEIME_SLEUTELS
        )
    return gevonden


def test_geen_taakhandler_zet_een_geheim_in_zijn_resultaat() -> None:
    """Een taakresultaat wordt bij voltooien niet geredigeerd, dus hier hoort niets in.

    Deze toets staat naast het weghalen van de return-waarde van de realmaanmaak: zonder
    hem kan een handler die waarde later alsnog doorgeven.
    """
    assert HANDLERS, "geen task_handlers-bestanden gevonden"
    aangetroffen = {bestand.name: _geheime_sleutels_in_returns(bestand) for bestand in HANDLERS}

    assert {naam: rijen for naam, rijen in aangetroffen.items() if rijen} == {}
