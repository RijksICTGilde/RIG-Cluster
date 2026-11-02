"""Tests for the project-KEK path through the two cluster migration scripts (RC-334).

Step 7 of the plan: ``migrate_project_to_production.migrate`` and
``migrate_project_to_sandbox.migrate_project`` carry ``config.age-private-key`` through
``unwrap_project_key_sync``/``wrap_project_key_sync`` instead of a direct
``decrypt_age_content_sync``/``encrypt_age_content_sync`` pair, so a KEK on either side
of the migration is used instead of silently ignored. Neither function had a test
before this change.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from opi.utils.age import decrypt_age_content_sync, encrypt_age_content_sync, wrap_project_key_sync

if TYPE_CHECKING:
    from collections.abc import Callable

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from migrate_project_to_production import migrate as migrate_to_production  # noqa: E402
from migrate_project_to_sandbox import migrate_project as migrate_to_sandbox  # noqa: E402


@pytest.fixture
def four_keypairs(
    make_age_keypair: Callable[[], tuple[str, str]],
) -> dict[str, tuple[str, str]]:
    """Distinct sandbox/prod platform and KEK keypairs, so a mix-up is visible."""
    return {
        "sandbox_platform": make_age_keypair(),
        "sandbox_kek": make_age_keypair(),
        "prod_platform": make_age_keypair(),
        "prod_kek": make_age_keypair(),
    }


def _minimal_project(project_public: str, wrapped_private_key: str) -> dict:
    return {
        "name": "test-project",
        "config": {"age-public-key": project_public, "age-private-key": wrapped_private_key},
        "deployments": [],
        "services": [],
    }


class TestMigrateToProduction:
    def test_without_a_kek_the_platform_keys_do_the_whole_round_trip(
        self, four_keypairs: dict[str, tuple[str, str]], make_age_keypair: Callable[[], tuple[str, str]]
    ):
        project_private, project_public = make_age_keypair()
        sandbox_private, sandbox_public = four_keypairs["sandbox_platform"]
        prod_private, prod_public = four_keypairs["prod_platform"]
        wrapped_on_sandbox = encrypt_age_content_sync(project_private, sandbox_public)
        data = _minimal_project(project_public, wrapped_on_sandbox)

        result = migrate_to_production(data, sandbox_private, prod_public, "rijksapp.dev", [], {})

        reopened = decrypt_age_content_sync(str(result["config"]["age-private-key"]), prod_private)
        assert reopened == project_private

    def test_with_a_kek_on_both_sides_the_kek_does_the_round_trip_not_the_platform_key(
        self, four_keypairs: dict[str, tuple[str, str]], make_age_keypair: Callable[[], tuple[str, str]]
    ):
        project_private, project_public = make_age_keypair()
        sandbox_private, _sandbox_public = four_keypairs["sandbox_platform"]
        sandbox_kek_private, sandbox_kek_public = four_keypairs["sandbox_kek"]
        prod_private, prod_public = four_keypairs["prod_platform"]
        prod_kek_private, prod_kek_public = four_keypairs["prod_kek"]
        # Already rewrapped under the sandbox KEK, as RC-334's own rotation round would leave it.
        wrapped_on_sandbox = wrap_project_key_sync(project_private, sandbox_kek_public)
        data = _minimal_project(project_public, wrapped_on_sandbox)

        result = migrate_to_production(
            data,
            sandbox_private,
            prod_public,
            "rijksapp.dev",
            [],
            {},
            sandbox_kek_private=sandbox_kek_private,
            prod_kek_public=prod_kek_public,
        )

        wrapped_result = str(result["config"]["age-private-key"])
        assert decrypt_age_content_sync(wrapped_result, prod_kek_private) == project_private
        assert decrypt_age_content_sync(wrapped_result, prod_private) is None

    def test_a_kek_only_source_cannot_be_unwrapped_without_the_sandbox_kek(
        self, four_keypairs: dict[str, tuple[str, str]], make_age_keypair: Callable[[], tuple[str, str]]
    ):
        project_private, project_public = make_age_keypair()
        sandbox_private, _sandbox_public = four_keypairs["sandbox_platform"]
        _sandbox_kek_private, sandbox_kek_public = four_keypairs["sandbox_kek"]
        _prod_private, prod_public = four_keypairs["prod_platform"]
        wrapped_on_sandbox = wrap_project_key_sync(project_private, sandbox_kek_public)
        data = _minimal_project(project_public, wrapped_on_sandbox)

        with pytest.raises(RuntimeError, match="Could not unwrap"):
            migrate_to_production(data, sandbox_private, prod_public, "rijksapp.dev", [], {})


class TestMigrateToSandbox:
    def test_without_a_kek_the_platform_keys_do_the_whole_round_trip(
        self, four_keypairs: dict[str, tuple[str, str]], make_age_keypair: Callable[[], tuple[str, str]]
    ):
        project_private, project_public = make_age_keypair()
        prod_private, prod_public = four_keypairs["prod_platform"]
        sandbox_private, sandbox_public = four_keypairs["sandbox_platform"]
        wrapped_on_prod = encrypt_age_content_sync(project_private, prod_public)
        data = _minimal_project(project_public, wrapped_on_prod)

        result = migrate_to_sandbox(data, prod_private, sandbox_public)

        # RC-288: de sandbox krijgt een EIGEN projectsleutel, dus de bewaarde sleutel is een
        # nieuwe. Gemeten wordt waarmee hij is ingepakt, niet of hij gelijk is gebleven.
        reopened = decrypt_age_content_sync(str(result["config"]["age-private-key"]), sandbox_private)
        assert reopened is not None
        assert reopened != project_private
        assert encrypt_age_content_sync("x", str(result["config"]["age-public-key"]))

    def test_with_a_kek_on_both_sides_the_kek_does_the_round_trip_not_the_platform_key(
        self, four_keypairs: dict[str, tuple[str, str]], make_age_keypair: Callable[[], tuple[str, str]]
    ):
        project_private, project_public = make_age_keypair()
        prod_private, _prod_public = four_keypairs["prod_platform"]
        prod_kek_private, prod_kek_public = four_keypairs["prod_kek"]
        sandbox_private, _sandbox_public = four_keypairs["sandbox_platform"]
        sandbox_kek_private, sandbox_kek_public = four_keypairs["sandbox_kek"]
        wrapped_on_prod = wrap_project_key_sync(project_private, prod_kek_public)
        data = _minimal_project(project_public, wrapped_on_prod)

        result = migrate_to_sandbox(
            data,
            prod_private,
            four_keypairs["sandbox_platform"][1],
            prod_kek_private=prod_kek_private,
            sandbox_kek_public=sandbox_kek_public,
        )

        wrapped_result = str(result["config"]["age-private-key"])
        # De nieuwe projectsleutel van RC-288 staat onder de KEK, niet onder de platformsleutel.
        nieuwe_sleutel = decrypt_age_content_sync(wrapped_result, sandbox_kek_private)
        assert nieuwe_sleutel is not None
        assert nieuwe_sleutel != project_private
        assert decrypt_age_content_sync(wrapped_result, sandbox_private) is None

    def test_a_kek_only_source_cannot_be_unwrapped_without_the_prod_kek(
        self, four_keypairs: dict[str, tuple[str, str]], make_age_keypair: Callable[[], tuple[str, str]]
    ):
        project_private, project_public = make_age_keypair()
        prod_private, _prod_public = four_keypairs["prod_platform"]
        _prod_kek_private, prod_kek_public = four_keypairs["prod_kek"]
        sandbox_public = four_keypairs["sandbox_platform"][1]
        wrapped_on_prod = wrap_project_key_sync(project_private, prod_kek_public)
        data = _minimal_project(project_public, wrapped_on_prod)

        with pytest.raises(RuntimeError, match="Failed to unwrap"):
            migrate_to_sandbox(data, prod_private, sandbox_public)
