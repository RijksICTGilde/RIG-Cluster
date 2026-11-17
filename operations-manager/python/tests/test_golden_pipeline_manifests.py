"""Golden test on the whole manifest pipeline: ``create_application_manifests``.

``tests/test_golden_manifests.py`` renders single templates against a hand-built variables
dict, so it says nothing about the 1.391 lines that ASSEMBLE that dict. This harness runs the
real method for a matrix of project files and byte-compares the output tree: the context
assembly, the ingress branches, the secret writers and the per-deployment NetworkPolicy. The
returned file list goes into the golden too, as ``created-files.txt``, because the
obsolete-manifest prune one level up works off it.

The method writes plaintext (``*.to-sops.yaml``); SOPS encryption happens later, in
``_process_deployment_manifests``, so the output here is byte-stable. Time is frozen and the
AGE keys are minted per run, so nothing key-derived reaches the output. Every stand-in that
cannot be avoided asserts it was reached: the subdomain registry (a database), the oauth2-proxy
cookie secret, the chart clone and the SOPS step.

Regenerating the goldens after an intended change::

    task golden-pipeline-baseline        # or: UPDATE_GOLDEN=1 uv run pytest tests/test_golden_pipeline_manifests.py

Read the resulting diff line by line. An unexpected change here is exactly the regression
this harness exists to catch.
"""

from __future__ import annotations

import os
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from freezegun import freeze_time
from opi.core.config import settings
from opi.manager.project_manager import ProjectManager
from opi.services.project_schema import validate_project_schema
from opi.services.secrets import DatabaseSecret, KeycloakSecret, MinIOSecret, RedisSecret
from opi.services.services import service_entry_name
from opi.utils.age import encrypt_age_content_sync
from opi.utils.naming import generate_deployment_manifest_path
from ruamel.yaml import YAML

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

GOLDEN_DIR = Path(__file__).parent / "golden" / "pipeline"
HELM_GOLDEN_DIR = Path(__file__).parent / "golden" / "pipeline-helm"
UPDATE_GOLDEN = os.environ.get("UPDATE_GOLDEN") == "1"

#: Frozen clock: the waker manifests stamp ``datetime.now(UTC)``.
FROZEN_NOW = "2026-01-02 03:04:05+00:00"

#: The authorization wall mints a fresh oauth2-proxy cookie secret on a first render (a later
#: render keeps the stored one). Pinned, so the output is comparable.
FIXED_COOKIE_SECRET = "golden-cookie-secret"  # a pinned test value, not a secret

#: The repository path the scenarios use, the third part of the manifest folder path.
REPO_PATH = "infra"

needs_age = pytest.mark.skipif(shutil.which("age-keygen") is None, reason="requires the age-keygen binary")


def _scenarios(root: Path = GOLDEN_DIR) -> list[str]:
    return sorted(path.name for path in root.iterdir() if path.is_dir())


class FakeSubdomainRegistry:
    """In-memory stand-in for the subdomain database, recording what was asked of it."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.registrations: dict[tuple[str, str], str] = {}

    async def get_by_deployment(self, project_name: str, deployment_name: str) -> dict[str, Any] | None:
        self.calls.append("get_by_deployment")
        subdomain = self.registrations.get((project_name, deployment_name))
        return {"subdomain": subdomain} if subdomain else None

    async def register_or_update_for_deployment(
        self, *, subdomain: str, base_domain: str, project_name: str, deployment_name: str, **_: Any
    ) -> None:
        self.calls.append("register_or_update_for_deployment")
        self.registrations[(project_name, deployment_name)] = subdomain

    async def register_bare_domain(self, *, base_domain: str, project_name: str, **_: Any) -> None:
        self.calls.append("register_bare_domain")

    async def delete_bare_domain(self, base_domain: str, *, project_name: str | None = None) -> bool:
        self.calls.append("delete_bare_domain")
        return False


@contextmanager
def _managed_cluster(cluster: str) -> Iterator[None]:
    """Run as the OPI instance of this cluster, which is how production runs.

    Without it the deployment sits on a cluster this instance does not manage, and
    ``get_deployment_by_name`` filters it away: the Helm values context then comes back EMPTY
    and every ``$ALIAS`` stays literal, without anything failing.
    """
    with patch.object(settings, "CLUSTER_MANAGER", cluster):
        yield


@pytest.fixture
def project_config(monkeypatch: pytest.MonkeyPatch, make_age_keypair: Callable[[], tuple[str, str]]) -> dict[str, str]:
    """A project AGE keypair, wrapped with a system key that only exists for this test.

    Every manifest path reads the project key (user env vars go through it even when there are
    none), and no key may sit in the tree: see ``make_age_keypair`` in conftest.
    """
    system_private, system_public = make_age_keypair()
    project_private, project_public = make_age_keypair()
    monkeypatch.setattr("opi.core.config.settings.SOPS_AGE_PRIVATE_KEY", system_private)
    return {
        "age-public-key": project_public,
        "age-private-key": encrypt_age_content_sync(project_private, system_public),
    }


#: Marker for a value the real file stores AGE-encrypted (a wake token, user env vars, an
#: attachment). The scenario writes the plaintext; the harness encrypts it with the project
#: key it minted, so no ciphertext and no key sits in the tree.
ENCRYPT_PREFIX = "ENCRYPT:"

#: Stand-in for an encrypted value when only the SHAPE matters (the schema check). Carries
#: no key and decrypts to nothing.
DUMMY_AGE_BLOCK = "-----BEGIN AGE ENCRYPTED FILE-----\ndummy\n-----END AGE ENCRYPTED FILE-----\n"


def _encrypt_markers(value: Any, public_key: str | None) -> Any:
    if isinstance(value, str) and value.startswith(ENCRYPT_PREFIX):
        plaintext = value[len(ENCRYPT_PREFIX) :]
        return encrypt_age_content_sync(plaintext, public_key) if public_key else DUMMY_AGE_BLOCK
    if isinstance(value, dict):
        return {key: _encrypt_markers(item, public_key) for key, item in value.items()}
    if isinstance(value, list):
        return [_encrypt_markers(item, public_key) for item in value]
    return value


def _load_project(scenario: str, project_config: dict[str, str], root: Path = GOLDEN_DIR) -> dict[str, Any]:
    project = YAML(typ="safe").load((root / scenario / "project.yaml").read_text())
    project = _encrypt_markers(project, project_config["age-public-key"])
    project["config"] = {**project.get("config", {}), **project_config}
    return project


def _seed_secrets(manager: ProjectManager, scenario: str, root: Path = GOLDEN_DIR) -> None:
    """Fill the secrets map the way the service managers do before manifest generation."""
    path = root / scenario / "secrets.yaml"
    if not path.exists():
        return
    classes = {
        "database": DatabaseSecret,
        "minio": MinIOSecret,
        "keycloak": KeycloakSecret,
        "redis": RedisSecret,
    }
    seeded = YAML(typ="safe").load(path.read_text())
    for deployment_name, per_service in seeded.items():
        for service, fields in per_service.items():
            manager._add_secret_to_create(deployment_name, service, classes[service](**fields))


async def _render(
    scenario: str, project_config: dict[str, str], output_root: Path, project: dict[str, Any] | None = None
) -> tuple[dict[str, str], list[str]]:
    """Run the real method for every deployment, and return the output tree plus its return value.

    ``project`` renders a variation on the scenario's file instead of the file itself, for a
    test that measures what one changed field does to the output.
    """
    project = project if project is not None else _load_project(scenario, project_config)
    registry = FakeSubdomainRegistry()

    with patch("opi.manager.project_manager.KubectlConnector"):
        manager = ProjectManager()

    manager.get_contents = AsyncMock(return_value=project)
    manager.get_name = AsyncMock(return_value=project["name"])
    _seed_secrets(manager, scenario)
    workspace = MagicMock()
    workspace.get_working_dir = AsyncMock(return_value=str(output_root))

    created: list[str] = []
    with (
        patch("opi.manager.project.manifests.application.SubdomainConnector", return_value=registry),
        patch("secrets.token_urlsafe", return_value=FIXED_COOKIE_SECRET) as token,
        freeze_time(FROZEN_NOW),
    ):
        for deployment in project["deployments"]:
            with _managed_cluster(deployment["cluster"]):
                # What _process_deployment_manifests does one step up, and what the secret
                # writers read: without it every alias in the fixture would be a no-op.
                manager._deployment_aliases[deployment["name"]] = await manager._collect_deployment_aliases(
                    deployment["name"]
                )
                target_dir = generate_deployment_manifest_path(
                    deployment["cluster"], project["name"], deployment["name"], REPO_PATH
                )
                created += await manager.create_application_manifests(deployment, workspace, target_dir)

    if _needs_registry(project):
        assert registry.calls, "the subdomain registry was patched but never used"
    if _needs_cookie_secret(project):
        token.assert_called()
    return _tree(output_root), created


def _needs_registry(project: dict[str, Any]) -> bool:
    """Whether any deployment names a domain of its own, the only reason to touch the registry."""
    return any("base-domain" in str(deployment) for deployment in project["deployments"])


def _needs_cookie_secret(project: dict[str, Any]) -> bool:
    """Whether a component sits behind the authorization wall, the only minter of a token."""
    return "authorization-wall" in str(project.get("components", []))


def _tree(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


#: The returned file list of ``create_application_manifests``, recorded next to the tree: the
#: obsolete-manifest prune works off it, so a change in it is a behaviour change.
CREATED_FILES_NAME = "created-files.txt"


def _write_golden(scenario: str, tree: dict[str, str], created: list[str] | None, root: Path = GOLDEN_DIR) -> None:
    expected = root / scenario / "expected"
    shutil.rmtree(expected, ignore_errors=True)
    expected.mkdir(parents=True)
    for name, content in tree.items():
        path = expected / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    if created is not None:
        (expected / CREATED_FILES_NAME).write_text("\n".join(created) + "\n", encoding="utf-8")


def _read_golden(scenario: str, root: Path = GOLDEN_DIR) -> tuple[dict[str, str], list[str] | None]:
    expected = root / scenario / "expected"
    tree = {
        str(path.relative_to(expected)): path.read_text(encoding="utf-8")
        for path in sorted(expected.rglob("*"))
        if path.is_file() and path.name != CREATED_FILES_NAME
    }
    created_file = expected / CREATED_FILES_NAME
    created = created_file.read_text(encoding="utf-8").split() if created_file.exists() else None
    return tree, created


@needs_age
@pytest.mark.parametrize("scenario", _scenarios())
@pytest.mark.asyncio
async def test_the_output_tree_matches_the_golden(
    scenario: str, project_config: dict[str, str], tmp_path: Path
) -> None:
    tree, created = await _render(scenario, project_config, tmp_path)

    if UPDATE_GOLDEN:
        _write_golden(scenario, tree, created)
        return

    expected_tree, expected_created = _read_golden(scenario)
    assert sorted(tree) == sorted(expected_tree), "a different set of files was written"
    for name in sorted(expected_tree):
        assert tree[name] == expected_tree[name], f"{scenario}/{name} differs from the golden"
    assert created == expected_created, "the returned list of created files changed"


@needs_age
@pytest.mark.parametrize("scenario", _scenarios())
@pytest.mark.asyncio
async def test_two_renders_are_identical(scenario: str, project_config: dict[str, str], tmp_path: Path) -> None:
    """Determinism, measured rather than assumed: no clock, no key and no set ordering leaks."""
    first, created_first = await _render(scenario, project_config, tmp_path / "een")
    second, created_second = await _render(scenario, project_config, tmp_path / "twee")

    assert first == second
    assert created_first == created_second


#: The scenario whose component mounts a volume above the free size, with the approval a
#: migrated file carries.
_APPROVED_STORAGE_SCENARIO = "bestaand-simple-example"
_APPROVED_STORAGE_PVC = "infra/local/example-project/just-a-name/frontend-data-pvc.yaml"


@needs_age
@pytest.mark.asyncio
async def test_a_volume_above_the_free_size_renders_on_its_approval(
    project_config: dict[str, str], tmp_path: Path
) -> None:
    """The fallback, measured on the real pipeline rather than on the resolver alone.

    Withdrawing the approval must lower the PVC to the free maximum, because that is what
    the project was allowed without a decision. The golden tree above is the other half: with
    the approval the volume renders on the 10Gi the component asks for.
    """
    approved, _ = await _render(_APPROVED_STORAGE_SCENARIO, project_config, tmp_path / "approved")
    assert 'storage: "10Gi"' in approved[_APPROVED_STORAGE_PVC]

    project = _load_project(_APPROVED_STORAGE_SCENARIO, project_config)
    service = next(entry for entry in project["services"] if service_entry_name(entry) == "persistent-storage")
    del service["config"]["size-approval"]
    without, _ = await _render(_APPROVED_STORAGE_SCENARIO, project_config, tmp_path / "without", project)

    assert 'storage: "2Gi"' in without[_APPROVED_STORAGE_PVC]


@pytest.mark.parametrize("scenario", _scenarios())
def test_the_scenario_is_a_valid_project_file(scenario: str) -> None:
    """A fixture the schema rejects measures a shape OPI would never process."""
    project = YAML(typ="safe").load((GOLDEN_DIR / scenario / "project.yaml").read_text())
    validate_project_schema(_encrypt_markers(project, None))


# ---------------------------------------------------------------------------
# The Helm paths: process_helm_chart_deployment and process_helmfile_deployment
# ---------------------------------------------------------------------------
#
# A separate matrix because a chart comes from another git repository. The clone is a fake
# that copies the scenario's own ``chart/`` tree into the working directory, so the values
# context, the secret manifests and the kustomization are the real thing. Only the SOPS step
# is patched: it rewrites the plaintext files into ciphertext that cannot be byte-compared,
# and every scenario asserts the patch was reached.


def _fake_git_connector(source: Path) -> MagicMock:
    """A GitConnector whose ``clone()`` fills the working directory from ``source``."""

    def factory(*, repo_url: str, branch: str, working_dir: str) -> MagicMock:
        connector = MagicMock()
        connector.clone = AsyncMock(side_effect=lambda: shutil.copytree(source, working_dir, dirs_exist_ok=True))
        connector.close = AsyncMock()
        return connector

    return MagicMock(side_effect=factory)


async def _render_helm(
    scenario: str, project_config: dict[str, str], output_root: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, str]:
    project = _load_project(scenario, project_config, HELM_GOLDEN_DIR)
    monkeypatch.setattr(settings, "RENDER_SOURCE_ALLOWLIST", "https://example.test", raising=False)

    with patch("opi.manager.project_manager.KubectlConnector"):
        manager = ProjectManager()
    manager.get_contents = AsyncMock(return_value=project)
    manager.get_name = AsyncMock(return_value=project["name"])
    _seed_secrets(manager, scenario, HELM_GOLDEN_DIR)

    workspace = MagicMock()
    workspace.get_working_dir = AsyncMock(return_value=str(output_root))

    git = _fake_git_connector(HELM_GOLDEN_DIR / scenario / "chart")
    with (
        patch("opi.manager.project.helm_deployment.GitConnector", git),
        patch("opi.manager.project.helm_deployment.encrypt_to_sops_files_or_fail") as encrypt,
        freeze_time(FROZEN_NOW),
    ):
        for deployment in project["deployments"]:
            target = output_root / deployment["name"]
            target.mkdir(parents=True, exist_ok=True)
            with _managed_cluster(deployment["cluster"]):
                if deployment.get("helmfile"):
                    await manager._process_helmfile_deployment(deployment, workspace, str(target))
                else:
                    await manager._process_helm_chart_deployment(deployment, workspace, str(target))

    assert git.called, "the clone was patched but never reached"
    assert encrypt.called, "the SOPS step was patched but never reached"
    return _tree(output_root)


@needs_age
@pytest.mark.parametrize("scenario", _scenarios(HELM_GOLDEN_DIR))
@pytest.mark.asyncio
async def test_the_helm_output_tree_matches_the_golden(
    scenario: str, project_config: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = await _render_helm(scenario, project_config, tmp_path, monkeypatch)

    if UPDATE_GOLDEN:
        _write_golden(scenario, tree, None, HELM_GOLDEN_DIR)
        return

    expected_tree, _ = _read_golden(scenario, HELM_GOLDEN_DIR)
    assert sorted(tree) == sorted(expected_tree), "a different set of files was written"
    for name in sorted(expected_tree):
        assert tree[name] == expected_tree[name], f"{scenario}/{name} differs from the golden"


@needs_age
@pytest.mark.parametrize("scenario", _scenarios(HELM_GOLDEN_DIR))
@pytest.mark.asyncio
async def test_two_helm_renders_are_identical(
    scenario: str, project_config: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = await _render_helm(scenario, project_config, tmp_path / "een", monkeypatch)
    second = await _render_helm(scenario, project_config, tmp_path / "twee", monkeypatch)

    assert first == second


@pytest.mark.parametrize("scenario", _scenarios(HELM_GOLDEN_DIR))
def test_the_helm_scenario_is_a_valid_project_file(scenario: str) -> None:
    project = YAML(typ="safe").load((HELM_GOLDEN_DIR / scenario / "project.yaml").read_text())
    validate_project_schema(_encrypt_markers(project, None))
