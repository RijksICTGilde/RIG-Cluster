"""Regression tests for _collect_deployment_aliases (conflicts, scoping, storage shape).

Aliases are declared per component but merged into ONE shared secret per service for
the deployment. Two components declaring the same alias name is expected for shared
services and fine when the value matches (logged at DEBUG). A DIFFERENT value is
unresolvable - a single shared secret cannot hold two values - so it must raise.
"""

import re
import shutil
import subprocess

import pytest
from opi.manager.project_manager import ProjectManager
from opi.services.component_values import encode as encode_component_values
from opi.utils.age import encrypt_age_content_sync


def _pm(component_defs: dict[str, dict]) -> ProjectManager:
    """A ProjectManager with just the three methods _collect_deployment_aliases uses."""
    pm = ProjectManager.__new__(ProjectManager)

    async def get_contents(*, record_base: bool = True) -> dict:
        # record_base mirrors the real signature: projection helpers such as
        # _collect_deployment_aliases read without recording a compare-and-swap base.
        return {"deployments": [{"name": "dep1", "components": [{"reference": n} for n in component_defs]}]}

    async def get_by_json_path(expr: str) -> dict:
        name = re.search(r"name=='([^']+)'", expr).group(1)
        return component_defs[name]

    pm.get_contents = get_contents  # type: ignore[method-assign]
    pm._get_by_json_path = get_by_json_path  # type: ignore[method-assign]
    pm._categorize_alias = lambda _name, _tmpl: ("database", "secret")  # type: ignore[method-assign]
    return pm


@pytest.mark.asyncio
async def test_identical_alias_across_components_is_allowed() -> None:
    pm = _pm(
        {
            "a": {"name": "a", "aliases": {"POSTGRES_DB": "{{ db.name }}"}},
            "b": {"name": "b", "aliases": {"POSTGRES_DB": "{{ db.name }}"}},
        }
    )
    result = await pm._collect_deployment_aliases("dep1")
    assert result["secret"]["database"]["POSTGRES_DB"] == "{{ db.name }}"


@pytest.mark.asyncio
async def test_conflicting_alias_across_components_raises() -> None:
    pm = _pm(
        {
            "a": {"name": "a", "aliases": {"POSTGRES_DB": "{{ db.name }}"}},
            "b": {"name": "b", "aliases": {"POSTGRES_DB": "{{ db.other }}"}},
        }
    )
    with pytest.raises(ValueError, match="Conflicting alias 'POSTGRES_DB'"):
        await pm._collect_deployment_aliases("dep1")


# ---------------------------------------------------------------------------
# Direct aliases are per component, not per deployment
# ---------------------------------------------------------------------------

PROJECT_DATA = {
    "components": [
        {
            "name": "headscale",
            "aliases": {
                "HEADSCALE_SERVER_URL": "https://${PUBLIC_HOSTNAME}",
                "HEADSCALE_NOISE_PRIVATE_KEY_PATH": "${DATA_PATH}/noise_private.key",
            },
        },
        # No services and no aliases: this is the component that used to blow up.
        {"name": "vlam-gateway"},
    ]
}

DEPLOYMENT_WIDE_DIRECT = {
    "web": {"HEADSCALE_SERVER_URL": "https://${PUBLIC_HOSTNAME}"},
    "storage": {"HEADSCALE_NOISE_PRIVATE_KEY_PATH": "${DATA_PATH}/noise_private.key"},
}


def test_declaring_component_keeps_its_direct_aliases() -> None:
    scoped = ProjectManager._scope_direct_aliases_to_component(DEPLOYMENT_WIDE_DIRECT, PROJECT_DATA, "headscale")
    assert scoped == DEPLOYMENT_WIDE_DIRECT


def test_sibling_without_aliases_gets_none() -> None:
    """A component that declares no aliases must not inherit a sibling's.

    Direct aliases resolve against the component's own env vars. Passing the
    deployment-wide set to a component with neither publish-on-web nor storage
    resolved them against an empty context and raised.
    """
    scoped = ProjectManager._scope_direct_aliases_to_component(DEPLOYMENT_WIDE_DIRECT, PROJECT_DATA, "vlam-gateway")
    assert scoped == {}


def test_partial_overlap_keeps_only_declared_aliases() -> None:
    project_data = {"components": [{"name": "worker", "aliases": {"WORKER_PATH": "${DATA_PATH}/w"}}]}
    direct = {
        "storage": {"WORKER_PATH": "${DATA_PATH}/w", "OTHER_PATH": "${DATA_PATH}/o"},
        "web": {"OTHER_URL": "https://${PUBLIC_HOSTNAME}"},
    }
    scoped = ProjectManager._scope_direct_aliases_to_component(direct, project_data, "worker")
    # Empty categories are dropped entirely, not left as {}.
    assert scoped == {"storage": {"WORKER_PATH": "${DATA_PATH}/w"}}


# ---------------------------------------------------------------------------
# The storage shape must not change what the deploy path sees (RC-106)
# ---------------------------------------------------------------------------
#
# Aliases used to be stored as a mapping with each value AGE-encrypted on its own;
# they are now ONE AGE block whose plaintext is KEY=value lines. Everything above
# reads them as a mapping, which is exactly how an alias falls away silently: with a
# block, ``.items()`` on the stored value walks a ciphertext instead of the aliases.
# A missing alias is not a failure anybody sees here -- it is a running pod without
# the environment variable, discovered when the application falls over. So the gate
# is a comparison: the same aliases, stored both ways, must produce the same answer.

_HAS_AGE = shutil.which("age") is not None and shutil.which("age-keygen") is not None


def _keypair() -> tuple[str, str]:
    result = subprocess.run(["age-keygen"], capture_output=True, text=True, check=True)
    lines = result.stdout.splitlines() + result.stderr.splitlines()
    private_key = next(line for line in lines if line.startswith("AGE-SECRET-KEY"))
    public_key = next(line.split(": ", 1)[1].strip() for line in lines if "public key:" in line.lower())
    return public_key, private_key


@pytest.fixture
def age_project(monkeypatch) -> dict:
    """A project whose AGE keypair is real, so a stored block really is encrypted."""
    system_public, system_private = _keypair()
    project_public, project_private = _keypair()
    monkeypatch.setattr("opi.core.config.settings.SOPS_AGE_PRIVATE_KEY", system_private)
    return {
        "name": "demo",
        "config": {
            "age-public-key": project_public,
            "age-private-key": encrypt_age_content_sync(project_private, system_public),
        },
    }


ALIASES = {
    "POSTGRES_DB": "{{ db.name }}",
    "POSTGRES_USER": "{{ db.user }}",
}


def _pm_with_config(component_defs: dict[str, dict], project: dict) -> ProjectManager:
    """Like ``_pm``, but the project carries the AGE config a block needs to be read."""
    pm = ProjectManager.__new__(ProjectManager)

    async def get_contents(*, record_base: bool = True) -> dict:
        return {
            **project,
            "components": list(component_defs.values()),
            "deployments": [{"name": "dep1", "components": [{"reference": n} for n in component_defs]}],
        }

    async def get_by_json_path(expr: str) -> dict:
        name = re.search(r"name=='([^']+)'", expr).group(1)
        return component_defs[name]

    pm.get_contents = get_contents  # type: ignore[method-assign]
    pm._get_by_json_path = get_by_json_path  # type: ignore[method-assign]
    pm._categorize_alias = lambda _name, _tmpl: ("database", "secret")  # type: ignore[method-assign]
    return pm


@pytest.mark.skipif(not _HAS_AGE, reason="age/age-keygen binary not available")
@pytest.mark.asyncio
async def test_a_block_and_an_unencrypted_mapping_deploy_the_same_aliases(age_project: dict) -> None:
    block = encode_component_values(ALIASES, age_project)
    assert isinstance(block, str), "the fixture must really be one block, or this proves nothing"

    from_block = await _pm_with_config({"a": {"name": "a", "aliases": block}}, age_project)._collect_deployment_aliases(
        "dep1"
    )
    from_mapping = await _pm_with_config(
        {"a": {"name": "a", "aliases": dict(ALIASES)}}, age_project
    )._collect_deployment_aliases("dep1")

    assert from_block["secret"]["database"] == ALIASES
    assert from_block == from_mapping


@pytest.mark.skipif(not _HAS_AGE, reason="age/age-keygen binary not available")
@pytest.mark.asyncio
async def test_a_per_value_encrypted_mapping_still_deploys(age_project: dict) -> None:
    """The shape written before RC-106. Read, never written again -- but it must deploy."""
    public = age_project["config"]["age-public-key"]
    stored = {name: encrypt_age_content_sync(value, public) for name, value in ALIASES.items()}

    result = await _pm_with_config({"a": {"name": "a", "aliases": stored}}, age_project)._collect_deployment_aliases(
        "dep1"
    )

    assert result["secret"]["database"] == ALIASES


@pytest.mark.skipif(not _HAS_AGE, reason="age/age-keygen binary not available")
def test_scoping_direct_aliases_reads_the_block_and_not_its_ciphertext(age_project: dict) -> None:
    # The membership test below is ``name in own_aliases``. On a stored block that is a
    # substring test on armored base64, so every direct alias silently drops out.
    direct = {"database": dict(ALIASES)}
    project_data = {
        **age_project,
        "components": [{"name": "a", "aliases": encode_component_values(ALIASES, age_project)}],
    }

    scoped = ProjectManager._scope_direct_aliases_to_component(direct, project_data, "a")

    assert scoped == direct
