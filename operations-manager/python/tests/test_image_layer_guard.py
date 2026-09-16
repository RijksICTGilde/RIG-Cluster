"""Tests for the layer layout of operations-manager/Dockerfile.

What matters is not the total size but how much of it an ordinary code change rebuilds.
The four regressions guarded here are silent: they cost size, never a failing test.
Background and measurements: features/image-lagen.md.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCKERFILE = REPO_ROOT / "operations-manager" / "Dockerfile"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"
STATIC_DIR = REPO_ROOT / "operations-manager" / "python" / "static"

APP_STAGE = "application"


def _instructions(text: str) -> list[tuple[str, str]]:
    """The Dockerfile as (stage, instruction) pairs, continuations joined, comments gone."""
    joined: list[str] = []
    buffer = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            buffer += line[:-1] + " "
            continue
        joined.append(buffer + line)
        buffer = ""
    if buffer:
        joined.append(buffer)

    stage = ""
    pairs: list[tuple[str, str]] = []
    for instruction in joined:
        if instruction.upper().startswith("FROM "):
            match = re.search(r"\bAS\s+(\S+)", instruction, re.IGNORECASE)
            stage = match.group(1) if match else ""
        pairs.append((stage, instruction))
    return pairs


@pytest.fixture(scope="module")
def instructions() -> list[tuple[str, str]]:
    return _instructions(DOCKERFILE.read_text())


@pytest.fixture(scope="module")
def app_stage(instructions: list[tuple[str, str]]) -> list[str]:
    return [instruction for stage, instruction in instructions if stage == APP_STAGE]


def _copy_sources(instruction: str) -> list[str]:
    """The source arguments of a COPY, without its flags and without the destination."""
    parts = instruction.split()[1:]
    parts = [part for part in parts if not part.startswith("--")]
    return parts[:-1]


def _copy_dest(instruction: str) -> str:
    return instruction.split()[-1]


def _copies(app_stage: list[str]) -> list[tuple[int, str]]:
    return [(index, line) for index, line in enumerate(app_stage) if line.upper().startswith("COPY ")]


class TestDockerignore:
    def test_node_modules_is_excluded_at_every_depth(self) -> None:
        """Without `**/` the pattern is only matched against the root of the build context."""
        lines = [line.strip() for line in DOCKERIGNORE.read_text().splitlines()]

        assert "**/node_modules/" in lines
        assert "node_modules/" not in lines

    @pytest.mark.parametrize("pattern", ["__pycache__/", "*.pyc", "*.pyo", "*.pyd"])
    def test_python_bytecode_is_excluded_at_every_depth(self, pattern: str) -> None:
        """Bytecode under opi/ changes on every test run and would rebuild the opi layer."""
        lines = [line.strip() for line in DOCKERIGNORE.read_text().splitlines()]

        assert f"**/{pattern}" in lines
        assert pattern not in lines

    def test_yaml_is_excluded_at_the_root_only(self) -> None:
        """opi/configs/*.yaml must reach the image, so this one must NOT get `**/`."""
        lines = [line.strip() for line in DOCKERIGNORE.read_text().splitlines()]

        assert "*.yaml" in lines
        assert "**/*.yaml" not in lines


class TestApplicationStage:
    def test_no_recursive_chown_or_chmod(self, app_stage: list[str]) -> None:
        """A recursive rewrite of /app duplicates every layer above it in a new layer."""
        assert app_stage, f"stage {APP_STAGE} not found"
        offenders = [line for line in app_stage if re.search(r"(chown|chmod)\s+-R", line)]

        assert offenders == []

    def test_every_copy_sets_ownership(self, app_stage: list[str]) -> None:
        """Ownership belongs on the COPY, which is what replaces the `chown -R` layer."""
        copies = [line for line in app_stage if line.upper().startswith("COPY ")]
        assert copies, "no COPY found in the application stage"

        missing = [line for line in copies if "--chown=appuser:appuser" not in line]
        assert missing == []

    def test_app_directory_itself_is_owned_by_the_user(self, app_stage: list[str]) -> None:
        """COPY --chown covers what it writes, not /app, which WORKDIR created as root."""
        user_setup = [line for line in app_stage if "useradd" in line]
        assert user_setup, "no user creation found in the application stage"

        assert any("chown appuser:appuser /app" in line for line in user_setup)

    def test_entrypoint_is_made_executable_after_it_is_copied(self, app_stage: list[str]) -> None:
        """The dropped `chmod -R 755 /app` ran last, so the order used to be free. It is not."""
        copied = [index for index, line in _copies(app_stage) if line.endswith("/app/entrypoint.sh")]
        made_executable = [index for index, line in enumerate(app_stage) if "chmod +x /app/entrypoint.sh" in line]
        assert len(copied) == 1
        assert len(made_executable) == 1

        assert copied[0] < made_executable[0]

    def test_runs_as_uid_1001(self, app_stage: list[str]) -> None:
        """OpenShift compatibility hangs on the number; the name says nothing about the uid."""
        assert "USER appuser" in app_stage

        assert any("groupadd -g 1001 appuser" in line for line in app_stage)
        assert any("useradd" in line and "-u 1001" in line for line in app_stage)

    def test_a_copied_directory_keeps_its_own_name(self, app_stage: list[str]) -> None:
        """COPY writes the CONTENTS of a directory, so a dest of ./static drops css a level up."""
        for _, line in _copies(app_stage):
            dest = _copy_dest(line).rstrip("/")
            for source in _copy_sources(line):
                if (REPO_ROOT / source).is_dir():
                    assert dest.endswith("/" + Path(source).name), line

    def test_every_application_directory_is_copied(self, app_stage: list[str]) -> None:
        """Splitting the block means naming the parts, and a dropped part only fails at runtime."""
        sources = {source for _, line in _copies(app_stage) for source in _copy_sources(line)}

        required = {
            "operations-manager/python/alembic.ini",
            "operations-manager/docker-entrypoint.sh",
            "operations-manager/python/manifests",
            "operations-manager/python/opi",
        }
        assert required - sources == set()

    def test_every_copied_source_exists(self, app_stage: list[str]) -> None:
        """A COPY of a directory that is gone only fails at docker build, long after the merge."""
        missing = [
            source
            for _, line in _copies(app_stage)
            if "--from=" not in line
            for source in _copy_sources(line)
            if not any(REPO_ROOT.glob(source))
        ]
        assert missing == []


class TestStaticLayers:
    def _static_copies(self, app_stage: list[str]) -> list[tuple[int, list[str]]]:
        copies: list[tuple[int, list[str]]] = []
        for index, line in enumerate(app_stage):
            if not line.upper().startswith("COPY "):
                continue
            sources = [src for src in _copy_sources(line) if "python/static" in src]
            if sources:
                copies.append((index, sources))
        return copies

    def test_media_is_copied_before_the_files_that_change(self, app_stage: list[str]) -> None:
        """In one layer a changed stylesheet rebuilds the media that never changes with it."""
        copies = self._static_copies(app_stage)
        assert len(copies) > 1, "static is still copied in a single layer"

        media = [index for index, sources in copies if any(src.endswith((".mp4", ".jpg", ".ico")) for src in sources)]
        changing = [index for index, sources in copies if any(src.endswith(("/css", "/js")) for src in sources)]
        assert media
        assert changing

        assert max(media) < min(changing)

    def test_the_application_code_is_copied_last(self, app_stage: list[str]) -> None:
        """A rebuilt layer drags every layer below it along, so COPY opi must sit below the media."""
        copies = [index for index, line in enumerate(app_stage) if line.upper().startswith("COPY ")]
        opi_copy = [index for index, line in enumerate(app_stage) if line.endswith("./opi")]
        assert len(opi_copy) == 1

        assert opi_copy[0] == max(copies)

    def test_every_static_entry_is_copied(self, app_stage: list[str]) -> None:
        """Splitting the COPY means naming the parts, so a forgotten one is a 404 on the portal."""
        covered: set[Path] = set()
        for _, sources in self._static_copies(app_stage):
            for source in sources:
                covered.update(REPO_ROOT.glob(source))

        expected = {entry for entry in STATIC_DIR.iterdir() if entry.name != "node_modules"}
        missing = [entry for entry in expected if not any(path == entry or path in entry.parents for path in covered)]
        assert missing == []


class TestPinnedTools:
    def test_uv_is_pinned(self, instructions: list[tuple[str, str]]) -> None:
        """`uv:latest` invalidates its own layer and the `uv sync` after it."""
        uv_refs = [line for _, line in instructions if "astral-sh/uv" in line]
        assert uv_refs

        for line in uv_refs:
            assert ":latest" not in line
            assert "${UV_VERSION}" in line

        args = [line for _, line in instructions if line.startswith("ARG UV_VERSION=")]
        assert len(args) == 1
        assert args[0] != "ARG UV_VERSION="

    def test_no_image_from_outside_floats_on_latest(self, instructions: list[tuple[str, str]]) -> None:
        """A floating base rebuilds its own stage and every stage after it."""
        stages = {stage for stage, _ in instructions if stage}
        bases = [line.split()[1] for _, line in instructions if line.upper().startswith("FROM ")]

        external = [base for base in bases if base not in stages]
        assert external

        for base in external:
            image, _, tag = base.rpartition(":")
            assert image, f"{base} carries no tag, which means latest"
            assert tag != "latest", base


class TestAptLayers:
    def test_every_apt_install_uses_the_cache_mounts(self, instructions: list[tuple[str, str]]) -> None:
        """Without the mounts the package lists land in the layer and the download repeats."""
        installs = [line for _, line in instructions if line.upper().startswith("RUN ") and "apt-get install" in line]
        assert installs

        missing = [
            line
            for line in installs
            if "--mount=type=cache,target=/var/cache/apt" not in line
            or "--mount=type=cache,target=/var/lib/apt" not in line
        ]
        assert missing == []
