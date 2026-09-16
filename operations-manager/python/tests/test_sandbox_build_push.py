"""Tests voor de sandbox-build die naar de registry naast het kind-cluster pusht.

De build pusht `localhost:5001/operations-manager:<commit>` en het cluster pullt die zelf;
zie docs/sandbox-kind-registry.md. Bewaakt wordt:
  1. de tag per commit, ook voor een ongecommitte wijziging (anders rolt er niets uit);
  2. dat de overlay bij elke deploy op die tag gezet wordt, ook vanuit een oude checkout;
  3. dat er op het pad van de deploy geen `kind load` meer zit;
  4. dat skaffold naar dezelfde registry pusht en de overlay die naam ook draagt.

De shell uit de Taskfile wordt echt gedraaid in een tijdelijke git-repo.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
TASKFILE = REPO_ROOT / "Taskfile.yaml"
REGISTRY_SCRIPT = REPO_ROOT / "scripts" / "setup-kind-registry.sh"
OVERLAYS = REPO_ROOT / "bootstrap" / "rig-system" / "kustomize" / "operations-manager" / "overlays"
OM_PATCH_REL = "bootstrap/rig-system/kustomize/operations-manager/overlays/sandboxed-local/patches/deployment.yaml"
SKAFFOLD_FILES = [
    REPO_ROOT / "operations-manager" / "skaffold-sandboxed-local.yaml",
    REPO_ROOT / "operations-manager" / "skaffold-sandboxed-local-debug.yaml",
]
SKAFFOLD_OVERLAYS = ["sandboxed-local-dev", "sandboxed-local-debug"]

BUILD_TASK = "sandbox:build-operations-manager-image"
UPDATE_TASK = "sandbox:update-operations-manager"
CONFIGURE_TASK = "sandbox:configure-operations-manager-image"
CONFIGURE_ALL_TASK = "sandbox:configure-local-images"
REGISTRY_TASK = "sandbox:setup-registry"

REPO = "localhost:5001/operations-manager"
GHCR = "ghcr.io/minbzk/base-images/operations-manager/operations-manager"


@pytest.fixture(scope="module")
def taskfile() -> dict:
    return yaml.safe_load(TASKFILE.read_text())


@pytest.fixture(scope="module")
def tools() -> None:
    if shutil.which("bash") is None or shutil.which("git") is None:
        pytest.skip("bash of git ontbreekt")


def _cmds(taskfile: dict, name: str) -> str:
    return "\n".join(str(cmd) for cmd in taskfile["tasks"][name]["cmds"])


def _called_tasks(taskfile: dict, name: str) -> list[str]:
    """Taken die `name` aanroept: als `task:`-stap of als `task X` in de shell."""
    called = []
    for cmd in taskfile["tasks"][name].get("cmds", []):
        if isinstance(cmd, dict) and "task" in cmd:
            called.append(cmd["task"])
        elif isinstance(cmd, str):
            called += re.findall(r"(?m)^\s*task ([\w:.-]+)", cmd)
    return called


def _reachable(taskfile: dict, name: str) -> set[str]:
    seen: set[str] = set()
    todo = [name]
    while todo:
        current = todo.pop()
        if current in seen or current not in taskfile["tasks"]:
            continue
        seen.add(current)
        todo += _called_tasks(taskfile, current)
    return seen


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path, tools: None) -> Path:
    """Een git-repo met de overlay zoals hij in git staat en een bestand in opi/."""
    (tmp_path / "operations-manager/python/opi").mkdir(parents=True)
    (tmp_path / "operations-manager/python/opi/app.py").write_text("x = 1\n")
    patch = tmp_path / OM_PATCH_REL
    patch.parent.mkdir(parents=True)
    patch.write_text((REPO_ROOT / OM_PATCH_REL).read_text())
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    return tmp_path


def _tag(taskfile: dict, repo: Path) -> str:
    sh = taskfile["vars"]["SANDBOX_OM_TAG"]["sh"]
    return subprocess.run(["bash", "-c", sh], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def _configure(taskfile: dict, repo: Path) -> str:
    """Draait de configure-taak met de tag die de Taskfile voor deze boom berekent."""
    image = f"{REPO}:{_tag(taskfile, repo)}"
    script = _cmds(taskfile, CONFIGURE_TASK).replace("{{.SANDBOX_OM_IMAGE}}", image)
    subprocess.run(["bash", "-c", script], cwd=repo, check=True, capture_output=True, text=True)
    return image


def _overlay_copy(tmp_path: Path) -> Path:
    """Kopie van de operations-manager-overlays onder hun repopad, zonder de SOPS-generator (die vraagt een sleutel)."""
    if shutil.which("kustomize") is None:
        pytest.skip("kustomize ontbreekt")
    copy = tmp_path / OVERLAYS.parent.relative_to(REPO_ROOT)
    shutil.copytree(OVERLAYS.parent, copy)
    base = copy / "overlays" / "sandboxed-local" / "kustomization.yaml"
    data = yaml.safe_load(base.read_text())
    data.pop("generators")
    base.write_text(yaml.safe_dump(data))
    return copy


def _render(copy: Path, overlay: str) -> dict:
    out = subprocess.run(
        ["kustomize", "build", str(copy / "overlays" / overlay), "--load-restrictor", "LoadRestrictionsNone"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    deployment = next(
        doc
        for doc in yaml.safe_load_all(out)
        if doc and doc["kind"] == "Deployment" and doc["metadata"]["name"] == "operations-manager"
    )
    return deployment["spec"]["template"]["spec"]["containers"][0]


def _patched_container(repo: Path) -> dict:
    return yaml.safe_load((repo / OM_PATCH_REL).read_text())["spec"]["template"]["spec"]["containers"][0]


class TestImageTag:
    def test_image_is_repo_plus_tag(self, taskfile: dict) -> None:
        assert taskfile["vars"]["SANDBOX_OM_IMAGE"] == "{{.SANDBOX_OM_REPO}}:{{.SANDBOX_OM_TAG}}"
        assert f'"{REPO}"' in taskfile["vars"]["SANDBOX_OM_REPO"]

    def test_repo_port_matches_the_registry_script(self, taskfile: dict) -> None:
        port = re.search(r"KIND_REGISTRY_PORT:-(\d+)", REGISTRY_SCRIPT.read_text())

        assert port is not None
        assert f"localhost:{port.group(1)}/" in taskfile["vars"]["SANDBOX_OM_REPO"]

    def test_clean_tree_gets_the_short_commit(self, taskfile: dict, repo: Path) -> None:
        assert _tag(taskfile, repo) == _git(repo, "rev-parse", "--short", "HEAD")

    def test_uncommitted_change_gets_its_own_tag(self, taskfile: dict, repo: Path) -> None:
        """Zelfde tag na een wijziging betekent: geen rollout en IfNotPresent houdt het oude image."""
        head = _git(repo, "rev-parse", "--short", "HEAD")
        app = repo / "operations-manager/python/opi/app.py"

        app.write_text("x = 2\n")
        first = _tag(taskfile, repo)
        app.write_text("x = 3\n")
        second = _tag(taskfile, repo)

        assert first.startswith(f"{head}-dirty-")
        assert second.startswith(f"{head}-dirty-")
        assert first != second

    def test_untracked_new_file_gets_its_own_tag(self, taskfile: dict, repo: Path) -> None:
        """Een nieuw, nog niet toegevoegd bestand gaat wel mee in de build-context van docker."""
        head = _git(repo, "rev-parse", "--short", "HEAD")
        new = repo / "operations-manager/python/opi/nieuw.py"

        new.write_text("y = 1\n")
        first = _tag(taskfile, repo)
        new.write_text("y = 2\n")
        second = _tag(taskfile, repo)

        assert first.startswith(f"{head}-dirty-"), first
        assert first != second

    def test_staged_change_gets_its_own_tag(self, taskfile: dict, repo: Path) -> None:
        head = _git(repo, "rev-parse", "--short", "HEAD")
        (repo / "operations-manager/python/opi/app.py").write_text("x = 2\n")
        _git(repo, "add", ".")

        assert _tag(taskfile, repo).startswith(f"{head}-dirty-")

    def test_patched_overlay_does_not_change_the_tag(self, taskfile: dict, repo: Path) -> None:
        """De configure-taak schrijft zelf in bootstrap/; dat mag de tag van de build niet verschuiven."""
        before = _tag(taskfile, repo)
        _configure(taskfile, repo)

        assert _git(repo, "status", "--porcelain") != ""
        assert _tag(taskfile, repo) == before


class TestConfigureOverlay:
    def test_points_the_overlay_at_the_commit_image(self, taskfile: dict, repo: Path) -> None:
        image = _configure(taskfile, repo)

        assert _patched_container(repo)["image"] == image

    def test_rewrites_an_overlay_from_an_earlier_checkout(self, taskfile: dict, repo: Path) -> None:
        _configure(taskfile, repo)
        (repo / "operations-manager/python/opi/app.py").write_text("x = 2\n")
        _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "next")

        image = _configure(taskfile, repo)

        assert image == f"{REPO}:{_git(repo, 'rev-parse', '--short', 'HEAD')}"
        assert _patched_container(repo)["image"] == image

    def test_changes_only_the_image_line(self, taskfile: dict, repo: Path) -> None:
        _configure(taskfile, repo)

        diff = _git(repo, "diff", "--unified=0", "--", OM_PATCH_REL)
        changed = [line for line in diff.splitlines() if line[:1] in "+-" and not line.startswith(("+++", "---"))]
        assert len(changed) == 2
        assert all(re.match(r"^[+-] +image: ", line) for line in changed)

    def test_leaves_no_backup_file(self, taskfile: dict, repo: Path) -> None:
        _configure(taskfile, repo)

        assert list((repo / OM_PATCH_REL).parent.glob("*.bak")) == []

    def test_task_fills_in_repo_override_and_tag(self, repo: Path) -> None:
        """Via de echte `task`, dus ook de doorgifte SANDBOX_OM_REPO -> SANDBOX_OM_IMAGE -> sed."""
        task = shutil.which("task")
        if task is None:
            pytest.skip("task ontbreekt")
        shutil.copy(TASKFILE, repo / "Taskfile.yaml")
        (repo / "operations-manager/python/opi/app.py").write_text("x = 2\n")
        expected = f"localhost:5098/proef:{_tag(yaml.safe_load(TASKFILE.read_text()), repo)}"

        result = subprocess.run(
            [task, CONFIGURE_TASK],
            cwd=repo,
            env={**os.environ, "SANDBOX_OM_REPO": "localhost:5098/proef"},
            check=True,
            capture_output=True,
            text=True,
        )

        assert "-dirty-" in expected
        assert _patched_container(repo)["image"] == expected
        assert expected in result.stdout

    def test_deploy_overlay_renders_the_configured_image(self, taskfile: dict, tmp_path: Path) -> None:
        """De overlay die update-operations-manager toepast, mag de gezette tag niet overschrijven."""
        copy = _overlay_copy(tmp_path)
        image = f"{REPO}:abc1234-dirty-0123456"
        script = _cmds(taskfile, CONFIGURE_TASK).replace("{{.SANDBOX_OM_IMAGE}}", image)
        subprocess.run(["bash", "-c", script], cwd=tmp_path, check=True, capture_output=True, text=True)

        container = _render(copy, "sandboxed-local")

        assert container["image"] == image
        assert container["imagePullPolicy"] == "IfNotPresent"
        assert "overlays/sandboxed-local " in _cmds(taskfile, UPDATE_TASK)

    def test_overlay_pulls_if_not_present(self) -> None:
        """Never kan niet meer: de node moet het image uit de registry halen."""
        container = yaml.safe_load((REPO_ROOT / OM_PATCH_REL).read_text())["spec"]["template"]["spec"]["containers"][0]

        assert container["imagePullPolicy"] == "IfNotPresent"

    def test_dev_mode_configures_the_operations_manager_image(self, taskfile: dict) -> None:
        assert CONFIGURE_TASK in _called_tasks(taskfile, CONFIGURE_ALL_TASK)
        assert "operations-manager:latest" not in _cmds(taskfile, CONFIGURE_ALL_TASK)


class TestBuildPushes:
    def test_build_pushes_the_commit_image(self, taskfile: dict) -> None:
        cmds = _cmds(taskfile, BUILD_TASK)

        assert "--push" in cmds
        assert "-t {{.SANDBOX_OM_IMAGE}}" in cmds
        assert "--load" not in cmds

    def test_build_keeps_its_target(self, taskfile: dict) -> None:
        assert "--target application" in _cmds(taskfile, BUILD_TASK)

    def test_cache_stays_out_of_the_registry(self, taskfile: dict) -> None:
        code = [line for line in _cmds(taskfile, BUILD_TASK).splitlines() if not line.lstrip().startswith("#")]

        assert "--cache-from" in "\n".join(code)
        assert "--cache-to" not in "\n".join(code)

    def test_update_deploys_the_image_it_just_pushed(self, taskfile: dict) -> None:
        """Volgorde: registry, build, overlay op de tag, dan pas de apply."""
        cmds = taskfile["tasks"][UPDATE_TASK]["cmds"]
        steps = [{"task": REGISTRY_TASK}, {"task": BUILD_TASK}, {"task": CONFIGURE_TASK}]
        apply_index = next(i for i, cmd in enumerate(cmds) if isinstance(cmd, str) and "kubectl apply" in cmd)

        indexes = [cmds.index(step) for step in steps]
        assert indexes == sorted(indexes)
        assert indexes[-1] < apply_index

    def test_update_does_not_restart_but_waits_for_the_rollout(self, taskfile: dict) -> None:
        cmds = _cmds(taskfile, UPDATE_TASK)

        assert "rollout restart" not in cmds
        assert "rollout status deployment/operations-manager" in cmds

    @pytest.mark.parametrize("entry", [UPDATE_TASK, "sandbox:build-local-images-if-dev"])
    def test_no_kind_load_of_the_operations_manager(self, taskfile: dict, entry: str) -> None:
        for name in _reachable(taskfile, entry):
            assert not re.search(r"kind load docker-image operations-manager", _cmds(taskfile, name)), name

    def test_update_path_has_no_kind_load_at_all(self, taskfile: dict) -> None:
        reachable = _reachable(taskfile, UPDATE_TASK)

        assert {REGISTRY_TASK, BUILD_TASK, CONFIGURE_TASK} <= reachable
        for name in reachable:
            assert "kind load" not in _cmds(taskfile, name), name


class TestSkaffold:
    @pytest.mark.parametrize("path", SKAFFOLD_FILES, ids=lambda p: p.name)
    def test_pushes_to_the_registry(self, path: Path) -> None:
        build = yaml.safe_load(path.read_text())["build"]

        assert build["local"]["push"] is True
        assert [artifact["image"] for artifact in build["artifacts"]] == [REPO]

    @pytest.mark.parametrize("path", SKAFFOLD_FILES, ids=lambda p: p.name)
    def test_keeps_file_sync_and_context(self, path: Path) -> None:
        config = yaml.safe_load(path.read_text())
        sources = [rule["src"] for rule in config["build"]["artifacts"][0]["sync"]["manual"]]

        assert "operations-manager/python/opi/**/*.py" in sources
        assert config["deploy"]["kubeContext"] == "kind-rig-sandbox"

    @pytest.mark.parametrize("overlay", SKAFFOLD_OVERLAYS)
    def test_overlay_renames_the_published_image(self, overlay: str) -> None:
        """Skaffold vervangt alleen verwijzingen met de naam van zijn artifact."""
        kustomization = yaml.safe_load((OVERLAYS / overlay / "kustomization.yaml").read_text())

        assert {"name": GHCR, "newName": REPO} in kustomization["images"]

    @pytest.mark.parametrize("overlay", SKAFFOLD_OVERLAYS)
    def test_overlay_does_not_force_never(self, overlay: str) -> None:
        assert "Never" not in (OVERLAYS / overlay / "kustomization.yaml").read_text()

    @pytest.mark.parametrize("overlay", SKAFFOLD_OVERLAYS)
    def test_rendered_overlay_carries_the_artifact_name(self, overlay: str, tmp_path: Path) -> None:
        container = _render(_overlay_copy(tmp_path), overlay)

        assert container["image"].rsplit(":", 1)[0] == REPO
        assert container["imagePullPolicy"] == "IfNotPresent"
