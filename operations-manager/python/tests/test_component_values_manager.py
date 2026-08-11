"""The write layer behind the env-vars/aliases API (RC-55).

``ProjectManager.set_component_values`` is where the round trip actually touches a
project file: locate the component, decrypt what is there, apply the change, re-encrypt,
and commit only if something really changed. The commit path is stubbed at
``mutate_and_commit_project`` -- the store's own read-modify-write is covered by its own
tests -- but the change function itself runs for real, against the real ``age`` binary,
on a real-shaped project dict.

Two things are pinned here that nothing else can pin:

* the two layers stay SEPARATE in the file. They are merged at deploy time with the
  deployment-component winning per key, and that merge only means anything while both
  values are still stored in their own place. One service owning both layers is exactly
  the arrangement that could accidentally write them to one spot.
* an unchanged write commits nothing. AGE is not deterministic, so re-encrypting the
  same values would produce different ciphertext every call, and every call would land a
  commit in ``zad-projects``.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Any

import pytest
from opi.manager.project_manager import ProjectManager
from opi.services.catalog.base import ValueStorage
from opi.services.component_values import decode
from opi.utils.age import encrypt_age_content_sync

pytestmark = pytest.mark.skipif(
    shutil.which("age") is None or shutil.which("age-keygen") is None,
    reason="age/age-keygen binary not available",
)


def _keypair() -> tuple[str, str]:
    result = subprocess.run(["age-keygen"], capture_output=True, text=True, check=True)
    lines = result.stdout.splitlines() + result.stderr.splitlines()
    private_key = next(line for line in lines if line.startswith("AGE-SECRET-KEY"))
    public_key = next(line.split(": ", 1)[1].strip() for line in lines if "public key:" in line.lower())
    return public_key, private_key


class FakeCommit:
    """Stands in for the store: runs the change function, records what it produced.

    Mirrors the real contract exactly, including the part that matters most here --
    a change function returning None is a no-op that commits nothing.
    """

    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.commits: list[str] = []

    async def __call__(self, mutator, commit_message, **kwargs) -> bool:
        result = mutator(self.data)
        if result is None:
            return False
        self.data = result
        self.commits.append(commit_message)
        return True


@pytest.fixture
def project(monkeypatch) -> dict:
    system_public, system_private = _keypair()
    project_public, project_private = _keypair()
    monkeypatch.setattr("opi.core.config.settings.SOPS_AGE_PRIVATE_KEY", system_private)
    return {
        "schema-version": 2,
        "name": "demo",
        "config": {
            "age-public-key": project_public,
            "age-private-key": encrypt_age_content_sync(project_private, system_public),
        },
        "components": [{"name": "backend", "type": "single", "services": []}],
        "deployments": [
            {
                "name": "deployment-1",
                "cluster": "local",
                "namespace": "demo",
                "components": [{"reference": "backend"}],
            }
        ],
    }


@pytest.fixture
def manager(project: dict, monkeypatch) -> tuple[ProjectManager, FakeCommit]:
    instance = ProjectManager(project_file_relative_path="projects/demo.yaml")
    commit = FakeCommit(project)
    monkeypatch.setattr(instance, "mutate_and_commit_project", commit)
    return instance, commit


def _component(data: dict) -> dict:
    return data["components"][0]


def _deployment_component(data: dict) -> dict:
    return data["deployments"][0]["components"][0]


class TestTheServicesOwnRule:
    """The write path is held to the alias rule too, not only the API (RC-66)."""

    @pytest.mark.asyncio
    async def test_an_unknown_reference_is_refused_on_the_write_path(self, manager) -> None:
        instance, commit = manager

        result = await instance.set_component_values(
            "aliases", "component", "add", component_name="backend", values={"KAPOT": "$BESTAAT_ECHT_NIET"}
        )

        assert not result["success"]
        assert result["error_type"] == "invalid_values"
        assert "BESTAAT_ECHT_NIET" in result["error"]
        assert not commit.commits, "nothing may be committed for a refused value"

    @pytest.mark.asyncio
    async def test_an_own_env_var_with_a_dollar_is_still_stored(self, manager) -> None:
        instance, _ = manager

        result = await instance.set_component_values(
            "user-env-vars", "component", "add", component_name="backend", values={"PASSWORD": "p$BESTAAT_NIET"}
        )

        assert result["success"], result


class TestWritingOnTheComponent:
    @pytest.mark.asyncio
    async def test_add_stores_an_encrypted_block(self, manager) -> None:
        instance, commit = manager

        result = await instance.set_component_values(
            "user-env-vars", "component", "add", component_name="backend", values={"TOKEN": "rc55-secret"}
        )

        assert result["success"]
        assert result["changed"]
        stored = _component(commit.data)["user-env-vars"]
        assert "rc55-secret" not in str(stored), "the plaintext value is in the project file"
        assert decode(stored, ValueStorage.BLOCK, commit.data) == {"TOKEN": "rc55-secret"}

    @pytest.mark.asyncio
    async def test_add_then_patch_then_delete(self, manager) -> None:
        instance, commit = manager
        await instance.set_component_values(
            "user-env-vars", "component", "add", component_name="backend", values={"A": "1", "B": "2"}
        )
        await instance.set_component_values(
            "user-env-vars", "component", "patch", component_name="backend", values={"A": "9"}
        )
        await instance.set_component_values(
            "user-env-vars", "component", "delete", component_name="backend", keys=["B"]
        )

        assert decode(_component(commit.data)["user-env-vars"], ValueStorage.BLOCK, commit.data) == {"A": "9"}
        assert len(commit.commits) == 3

    @pytest.mark.asyncio
    async def test_clearing_removes_the_property_rather_than_storing_an_empty_one(self, manager) -> None:
        instance, commit = manager
        await instance.set_component_values(
            "user-env-vars", "component", "add", component_name="backend", values={"A": "1"}
        )

        await instance.set_component_values("user-env-vars", "component", "clear", component_name="backend")

        assert "user-env-vars" not in _component(commit.data)

    @pytest.mark.asyncio
    async def test_aliases_land_per_value_with_readable_names(self, manager) -> None:
        instance, commit = manager

        await instance.set_component_values(
            "aliases",
            "component",
            "add",
            component_name="backend",
            values={"POSTGRES_HOST": "$DATABASE_SERVER_HOST", "POSTGRES_PORT": "$DATABASE_SERVER_PORT"},
        )

        stored = _component(commit.data)["aliases"]
        assert sorted(stored) == ["POSTGRES_HOST", "POSTGRES_PORT"]
        assert all("BEGIN AGE ENCRYPTED FILE" in value for value in stored.values())

    @pytest.mark.asyncio
    async def test_a_component_that_is_not_there_fails_without_committing(self, manager) -> None:
        instance, commit = manager

        result = await instance.set_component_values(
            "user-env-vars", "component", "add", component_name="nope", values={"A": "1"}
        )

        assert not result["success"]
        assert result["error_type"] == "invalid_values"
        assert commit.commits == []


class TestTheTwoLayersStaySeparate:
    """Finding 5 of the 7 August sandbox run, measured on the layer this API writes."""

    @pytest.mark.asyncio
    async def test_writing_the_deployment_override_leaves_the_component_value_alone(self, manager) -> None:
        instance, commit = manager
        await instance.set_component_values(
            "user-env-vars", "component", "add", component_name="backend", values={"SHARED": "component-value"}
        )

        await instance.set_component_values(
            "user-env-vars",
            "deployment-component",
            "add",
            component_name="backend",
            deployment_name="deployment-1",
            values={"SHARED": "deployment-value"},
        )

        component_level = _component(commit.data).get("user-env-vars")
        deployment_level = _deployment_component(commit.data).get("user-env-vars")
        assert component_level, "writing the deployment override wiped the component-level value"
        assert deployment_level, "the deployment override never landed"
        assert decode(component_level, ValueStorage.BLOCK, commit.data) == {"SHARED": "component-value"}
        assert decode(deployment_level, ValueStorage.BLOCK, commit.data) == {"SHARED": "deployment-value"}

    @pytest.mark.asyncio
    async def test_clearing_one_layer_leaves_the_other(self, manager) -> None:
        instance, commit = manager
        await instance.set_component_values(
            "user-env-vars", "component", "add", component_name="backend", values={"A": "1"}
        )
        await instance.set_component_values(
            "user-env-vars",
            "deployment-component",
            "add",
            component_name="backend",
            deployment_name="deployment-1",
            values={"B": "2"},
        )

        await instance.set_component_values(
            "user-env-vars",
            "deployment-component",
            "clear",
            component_name="backend",
            deployment_name="deployment-1",
        )

        assert "user-env-vars" not in _deployment_component(commit.data)
        assert decode(_component(commit.data)["user-env-vars"], ValueStorage.BLOCK, commit.data) == {"A": "1"}

    @pytest.mark.asyncio
    async def test_a_component_missing_from_the_deployment_fails(self, manager) -> None:
        instance, commit = manager

        result = await instance.set_component_values(
            "user-env-vars",
            "deployment-component",
            "add",
            component_name="backend",
            deployment_name="nope",
            values={"A": "1"},
        )

        assert not result["success"]
        assert commit.commits == []


class TestNoChurn:
    @pytest.mark.asyncio
    async def test_patching_a_value_to_what_it_already_is_commits_nothing(self, manager) -> None:
        instance, commit = manager
        await instance.set_component_values(
            "user-env-vars", "component", "add", component_name="backend", values={"A": "1"}
        )
        before = _component(commit.data)["user-env-vars"]

        result = await instance.set_component_values(
            "user-env-vars", "component", "patch", component_name="backend", values={"A": "1"}
        )

        assert result["success"]
        assert result["changed"] is False
        assert len(commit.commits) == 1, "a no-op patch produced a second commit"
        assert _component(commit.data)["user-env-vars"] is before, "the ciphertext was rewritten for nothing"

    @pytest.mark.asyncio
    async def test_the_same_alias_value_twice_commits_nothing(self, manager) -> None:
        instance, commit = manager
        await instance.set_component_values(
            "aliases", "component", "add", component_name="backend", values={"HOST": "$DATABASE_SERVER_HOST"}
        )

        result = await instance.set_component_values(
            "aliases", "component", "patch", component_name="backend", values={"HOST": "$DATABASE_SERVER_HOST"}
        )

        assert result["changed"] is False
        assert len(commit.commits) == 1

    @pytest.mark.asyncio
    async def test_a_block_value_that_would_not_read_back_is_refused_here_too(self, manager) -> None:
        """The write path holds the same line-format rule the API does.

        ``KEY= x `` reads back as ``x``, so the stored set would never equal the requested
        one and the no-op check above could never be true again: every call would commit.
        Refusing it is what keeps that guarantee honest.
        """
        instance, commit = manager

        result = await instance.set_component_values(
            "user-env-vars", "component", "add", component_name="backend", values={"A": " x "}
        )

        assert not result["success"]
        assert result["error_type"] == "invalid_values"
        assert commit.commits == []

    @pytest.mark.asyncio
    async def test_edge_whitespace_is_refused_for_aliases_too(self, manager) -> None:
        # age decryption strips its plaintext, so PER_VALUE loses this as well.
        instance, commit = manager

        result = await instance.set_component_values(
            "aliases", "component", "add", component_name="backend", values={"A": " x "}
        )

        assert not result["success"]
        assert result["error_type"] == "invalid_values"
        assert commit.commits == []

    @pytest.mark.asyncio
    async def test_surrounding_quotes_are_refused_for_env_vars_but_kept_for_aliases(self, manager) -> None:
        instance, commit = manager

        refused = await instance.set_component_values(
            "user-env-vars", "component", "add", component_name="backend", values={"A": '"q"'}
        )
        assert not refused["success"]

        stored = await instance.set_component_values(
            "aliases", "component", "add", component_name="backend", values={"A": '"$DATABASE_SERVER_HOST"'}
        )
        assert stored["success"]
        assert stored["changed"]

        again = await instance.set_component_values(
            "aliases", "component", "patch", component_name="backend", values={"A": '"$DATABASE_SERVER_HOST"'}
        )
        assert again["changed"] is False, "PER_VALUE keeps the quotes, so re-writing it is a no-op"
        assert len(commit.commits) == 1

    @pytest.mark.asyncio
    async def test_clearing_what_is_already_empty_commits_nothing(self, manager) -> None:
        instance, commit = manager

        result = await instance.set_component_values("aliases", "component", "clear", component_name="backend")

        assert result["success"]
        assert result["changed"] is False
        assert commit.commits == []


class TestFailClosedOnTheWritePath:
    @pytest.mark.asyncio
    async def test_a_project_without_a_public_key_writes_nothing(self, manager) -> None:
        instance, commit = manager
        commit.data["config"].pop("age-public-key")

        result = await instance.set_component_values(
            "user-env-vars", "component", "add", component_name="backend", values={"A": "1"}
        )

        assert not result["success"]
        assert result["error_type"] == "invalid_values"
        assert commit.commits == []
        assert "user-env-vars" not in _component(commit.data)
