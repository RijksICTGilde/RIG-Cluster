"""De condities waarmee een route zich voor een step-up aanmeldt (RC-332).

Dekt ``opi/services/step_up_conditions.py``: de conditie leest de projectstore en krijgt
een lege projectdata voor wie er niet bij mag, zodat de dialoog niets verklapt over een
project waar je geen lid van bent.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from opi.services import step_up_conditions as conditions

OWNER = "eigenaar@example.org"
VREEMDE = "vreemde@example.org"


def _project_store(data: dict[str, Any] | None) -> MagicMock:
    store = MagicMock()
    store.get.return_value = SimpleNamespace(data=data) if data is not None else None
    return store


def _request(email: str) -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(user={"email": email}))


PROJECT = {
    "name": "demo",
    "deployments": [{"name": "prod", "environment": "productie", "components": [{"reference": "web"}]}],
}


def _patched(data: dict[str, Any] | None, *, geautoriseerd: bool = True):
    return (
        patch.object(conditions, "get_project_store", return_value=_project_store(data)),
        patch.object(conditions, "is_user_authorized_for_project", return_value=geautoriseerd),
    )


# ---------------------------------------------------------------------------
# deployment_environment_change
# ---------------------------------------------------------------------------


def test_downgrade_true_van_productie_naar_acceptatie() -> None:
    p1, p2 = _patched(PROJECT)
    with p1, p2:
        deployment_data = SimpleNamespace(environment="acceptatie", deploymentName="prod")
        assert (
            conditions.deployment_environment_change(
                _request(OWNER), project_name="demo", deployment_data=deployment_data
            )
            is True
        )


def test_verhogen_naar_productie_vraagt_ook_step_up() -> None:
    """Ontwerp paragraaf 7: de markering ZETTEN vraagt hetzelfde als hem verlagen."""
    project = {**PROJECT, "deployments": [{"name": "d1", "environment": "acceptatie"}]}
    p1, p2 = _patched(project)
    with p1, p2:
        deployment_data = SimpleNamespace(environment="productie", deploymentName="d1")
        assert (
            conditions.deployment_environment_change(
                _request(OWNER), project_name="demo", deployment_data=deployment_data
            )
            is True
        )


def test_downgrade_false_zonder_aangevraagde_markering() -> None:
    """Een aanroep die niets over de markering zegt laat de bestaande staan."""
    p1, p2 = _patched(PROJECT)
    with p1, p2:
        deployment_data = SimpleNamespace(environment=None, deploymentName="prod")
        assert (
            conditions.deployment_environment_change(
                _request(OWNER), project_name="demo", deployment_data=deployment_data
            )
            is False
        )


def test_nieuwe_deployment_op_de_standaard_vraagt_niets() -> None:
    """Een nieuwe deployment die op ``ontwikkeling`` blijft zet geen markering."""
    p1, p2 = _patched(PROJECT)
    with p1, p2:
        deployment_data = SimpleNamespace(environment="ontwikkeling", deploymentName="nieuw")
        assert (
            conditions.deployment_environment_change(
                _request(OWNER), project_name="demo", deployment_data=deployment_data
            )
            is False
        )


def test_nieuwe_deployment_meteen_op_productie_vraagt_step_up() -> None:
    """Hem meteen op productie zetten is net zo goed de markering zetten."""
    p1, p2 = _patched(PROJECT)
    with p1, p2:
        deployment_data = SimpleNamespace(environment="productie", deploymentName="nieuw")
        assert (
            conditions.deployment_environment_change(
                _request(OWNER), project_name="demo", deployment_data=deployment_data
            )
            is True
        )
