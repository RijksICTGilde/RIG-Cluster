"""De condities waarmee een route zich voor een step-up aanmeldt (RC-332).

Dekt ``opi/services/step_up_conditions.py``: elke conditie leest de projectstore en
geeft een lege projectdata terug voor wie er niet bij mag, zodat de dialoog niets
verklapt over een project waar je geen lid van bent.
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


def test_project_in_production_true_voor_een_productieproject() -> None:
    p1, p2 = _patched(PROJECT)
    with p1, p2:
        assert conditions.project_in_production(_request(OWNER), project_name="demo") is True


def test_project_in_production_false_voor_een_niet_productieproject() -> None:
    p1, p2 = _patched({"name": "demo", "deployments": [{"name": "d1", "environment": "test"}]})
    with p1, p2:
        assert conditions.project_in_production(_request(OWNER), project_name="demo") is False


def test_project_in_production_leest_leeg_voor_een_niet_geautoriseerde_gebruiker() -> None:
    """Een vreemde krijgt geen inhoud, dus ook geen bevestiging dat dit productie is."""
    p1, p2 = _patched(PROJECT, geautoriseerd=False)
    with p1, p2:
        assert conditions.project_in_production(_request(VREEMDE), project_name="demo") is False


def test_project_in_production_zonder_sessiegebruiker_leest_toch_de_store() -> None:
    """Een API-aanroep heeft geen sessiegebruiker: de projectsleutel is al gecontroleerd."""
    p1, p2 = _patched(PROJECT)
    with p1, p2:
        request = SimpleNamespace(state=SimpleNamespace())
        assert conditions.project_in_production(request, project_name="demo") is True


def test_project_in_production_onbekend_project_is_geen_productie() -> None:
    p1, p2 = _patched(None)
    with p1, p2:
        assert conditions.project_in_production(_request(OWNER), project_name="nope") is False


def test_deployment_in_production_true_voor_de_juiste_deployment() -> None:
    p1, p2 = _patched(PROJECT)
    with p1, p2:
        assert conditions.deployment_in_production(_request(OWNER), project_name="demo", deployment_name="prod") is True


def test_deployment_in_production_false_voor_een_andere_deployment() -> None:
    project = {**PROJECT, "deployments": [*PROJECT["deployments"], {"name": "acc", "environment": "acceptatie"}]}
    p1, p2 = _patched(project)
    with p1, p2:
        assert conditions.deployment_in_production(_request(OWNER), project_name="demo", deployment_name="acc") is False


def test_component_in_production_true_als_een_productiedeployment_het_gebruikt() -> None:
    p1, p2 = _patched(PROJECT)
    with p1, p2:
        assert conditions.component_in_production(_request(OWNER), project_name="demo", component_name="web") is True


def test_component_in_production_false_als_niemand_het_gebruikt() -> None:
    p1, p2 = _patched(PROJECT)
    with p1, p2:
        assert (
            conditions.component_in_production(_request(OWNER), project_name="demo", component_name="ongebruikt")
            is False
        )


# ---------------------------------------------------------------------------
# deployment_environment_downgrade
# ---------------------------------------------------------------------------


def test_downgrade_true_van_productie_naar_acceptatie() -> None:
    p1, p2 = _patched(PROJECT)
    with p1, p2:
        deployment_data = SimpleNamespace(environment="acceptatie", deploymentName="prod")
        assert (
            conditions.deployment_environment_downgrade(
                _request(OWNER), project_name="demo", deployment_data=deployment_data
            )
            is True
        )


def test_downgrade_false_van_acceptatie_naar_productie() -> None:
    project = {**PROJECT, "deployments": [{"name": "d1", "environment": "acceptatie"}]}
    p1, p2 = _patched(project)
    with p1, p2:
        deployment_data = SimpleNamespace(environment="productie", deploymentName="d1")
        assert (
            conditions.deployment_environment_downgrade(
                _request(OWNER), project_name="demo", deployment_data=deployment_data
            )
            is False
        )


def test_downgrade_false_zonder_aangevraagde_markering() -> None:
    """Een aanroep die niets over de markering zegt laat de bestaande staan."""
    p1, p2 = _patched(PROJECT)
    with p1, p2:
        deployment_data = SimpleNamespace(environment=None, deploymentName="prod")
        assert (
            conditions.deployment_environment_downgrade(
                _request(OWNER), project_name="demo", deployment_data=deployment_data
            )
            is False
        )


def test_downgrade_false_voor_een_nieuwe_deployment_die_nog_niet_bestaat() -> None:
    """Een nieuwe deployment kan niets verlagen: er is geen voor-staat om vanaf te verlagen."""
    p1, p2 = _patched(PROJECT)
    with p1, p2:
        deployment_data = SimpleNamespace(environment="test", deploymentName="nieuw")
        assert (
            conditions.deployment_environment_downgrade(
                _request(OWNER), project_name="demo", deployment_data=deployment_data
            )
            is False
        )
