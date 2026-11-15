"""Direct tests for ``categorize_alias``.

Every other test that drives ``collect_deployment_aliases`` stubs this function out
(the fixtures there carry no real platform variables), so its own four branches --
success, no reference, unknown variable, mixed services, mixed sources -- had no test
that ran its real logic.
"""

from __future__ import annotations

import pytest
from opi.manager.project import aliases as aliases_module
from opi.manager.project.aliases import categorize_alias


def test_a_secret_variable_categorizes_to_its_service_and_source() -> None:
    assert categorize_alias("POSTGRES_HOST", "$DATABASE_SERVER_HOST") == ("database", "secret")


def test_a_direct_variable_categorizes_to_its_service_and_source() -> None:
    assert categorize_alias("WORKER_PATH", "${DATA_PATH}/work") == ("storage", "direct")


def test_an_alias_without_any_reference_is_rejected() -> None:
    with pytest.raises(ValueError, match="no variable references"):
        categorize_alias("PLAIN", "not-a-reference")


def test_an_unknown_variable_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown variables: BESTAAT_ECHT_NIET"):
        categorize_alias("KAPOT", "$BESTAAT_ECHT_NIET")


def test_mixing_variables_from_two_services_is_rejected() -> None:
    with pytest.raises(ValueError, match="multiple services"):
        categorize_alias("MIXED", "$DATABASE_SERVER_HOST/$DATA_PATH")


def test_mixing_secret_and_direct_variables_of_one_service_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """No category in the current catalog mixes 'secret' and 'direct' (measured: every
    category is mono-source), so this branch needs a synthetic registry to prove it still
    guards the mix, rather than a real one that cannot reach it."""
    from opi.services.services import VariableDefinition
    from opi.services.services_enums import ServiceType

    fake_registry = {
        "FAKE_SECRET": (
            ServiceType.PLATFORM,
            VariableDefinition(name="FAKE_SECRET", description="test fixture", source="secret"),
        ),
        "FAKE_DIRECT": (
            ServiceType.PLATFORM,
            VariableDefinition(name="FAKE_DIRECT", description="test fixture", source="direct"),
        ),
    }
    monkeypatch.setattr(aliases_module, "variable_to_service", lambda: fake_registry)

    with pytest.raises(ValueError, match="mixes variables from different sources"):
        categorize_alias("MIXED_SOURCE", "$FAKE_SECRET/$FAKE_DIRECT")
