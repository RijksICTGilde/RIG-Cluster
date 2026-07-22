"""A decrypted secret must never reach git, on any write path.

``ProjectStore.get_decrypted()`` returns a throwaway copy with AGE blocks
decrypted, for display and editing. Its docstring says it must never be written
back -- but a docstring enforces nothing. The schema's ``age-encrypted`` pattern
does enforce it, except that ``_validate(enforce=False)`` used to catch the
schema error, log it and persist anyway. That flag exists so a recovery write is
not blocked by pre-existing structural drift; there are 11 call sites (auto-tune,
oom_watcher, restore, keycloak, backup), and each was a way for plaintext
credentials to land in the repository.
"""

from __future__ import annotations

import pytest
from opi.core.project_schema import ProjectSchemaError, find_plaintext_secret_violations

AGE_BLOCK = "-----BEGIN AGE ENCRYPTED FILE-----\nc29tZQ==\n-----END AGE ENCRYPTED FILE-----"


def _project(*, api_key: str = AGE_BLOCK, private_key: str = AGE_BLOCK) -> dict:
    return {
        "schema-version": 2,
        "name": "demo",
        "clusters": ["odcn-production"],
        "users": [{"email": "admin@rijksoverheid.nl", "role": "admin"}],
        "config": {
            "age-public-key": "age1qqqqqqqqqq",
            "age-private-key": private_key,
            "api-key": api_key,
        },
    }


def test_encrypted_project_has_no_violations() -> None:
    assert find_plaintext_secret_violations(_project()) == []


def test_decrypted_api_key_is_detected() -> None:
    violations = find_plaintext_secret_violations(_project(api_key="sk-live-plaintext-secret"))
    assert "config/api-key" in violations


def test_decrypted_private_key_is_detected() -> None:
    violations = find_plaintext_secret_violations(_project(private_key="AGE-SECRET-KEY-1PLAINTEXT"))
    assert "config/age-private-key" in violations


def test_all_decrypted_secrets_are_reported_together() -> None:
    violations = find_plaintext_secret_violations(_project(api_key="plain-key", private_key="plain-priv"))
    assert set(violations) >= {"config/api-key", "config/age-private-key"}


async def test_store_refuses_plaintext_secret_even_without_enforcement() -> None:
    """The guard that matters: enforce_validation=False must not open the door."""
    from opi.services.project_store import GitProjectStore

    store = GitProjectStore(working_dir="/tmp/unused-by-this-test")
    leaked = _project(api_key="sk-live-plaintext-secret")

    with pytest.raises(ProjectSchemaError, match="AGE-versleuteld"):
        await store._validate(leaked, enforce=False)


async def test_unrelated_drift_is_still_tolerated_without_enforcement() -> None:
    """The flag must keep doing its job: pre-existing drift does not block recovery."""
    from opi.services.project_store import GitProjectStore

    store = GitProjectStore(working_dir="/tmp/unused-by-this-test")
    drifted = _project()
    drifted["components"] = [{"name": "web", "type": "single", "uses-components": ["does-not-exist"]}]

    # Structurally inconsistent but no plaintext secret: tolerated, no raise.
    await store._validate(drifted, enforce=False)
