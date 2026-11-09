"""Chart- en helmfilepaden blijven binnen hun werkmap.

Bij een helm-chart of helmfile clonet OPI een externe repo naar een tijdelijke map en
kopieert er een deel van naar de deploymentmap. De padcomponenten daarvoor komen uit het
projectbestand en uit de ``Chart.yaml`` van die externe repo. ``os.path.join`` negeert
zijn eerste argument zodra het tweede absoluut is, en ``shutil.copytree`` volgde
standaard symlinks, dus beide konden buiten de werkmap wijzen.

Vastgezet wordt: een bron buiten de clone en een bestemming buiten ``charts_dir`` worden
geweigerd, een symlink die buiten de clone wijst ook, en een sentinel naast de werkmap
blijft onaangeroerd. Git is gemockt: de "clone" vult de tijdelijke map met een fixture.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.core.config import settings
from opi.manager.project.helm_deployment import clone_helm_chart, clone_helmfile_source
from opi.services.project_schema import ProjectSchemaError, validate_project_schema
from opi.utils.fs_paths import reject_escaping_symlinks, safe_join, single_segment

if TYPE_CHECKING:
    from collections.abc import Callable


@pytest.fixture(autouse=True)
def _allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    """De clone-routes toetsen hun bron tegen de allowlist, en deze fixtures klonen van example.test."""
    monkeypatch.setattr(settings, "RENDER_SOURCE_ALLOWLIST", "https://example.test", raising=False)


def _fake_git_connector(fill: Callable[[Path], None]) -> MagicMock:
    """Een GitConnector-fabriek waarvan ``clone()`` de werkmap met een fixture vult."""

    def factory(*, repo_url: str, branch: str, working_dir: str) -> MagicMock:
        connector = MagicMock()
        connector.clone = AsyncMock(side_effect=lambda: fill(Path(working_dir)))
        connector.close = AsyncMock()
        return connector

    return MagicMock(side_effect=factory)


def _chart(root: Path, chart_yaml: str) -> None:
    chart = root / "chart"
    chart.mkdir()
    (chart / "Chart.yaml").write_text(chart_yaml)
    (chart / "values.yaml").write_text("replicas: 1\n")


# --- de helper zelf -------------------------------------------------------------------


def test_safe_join_geeft_een_gewone_submap(tmp_path: Path) -> None:
    (tmp_path / "chart").mkdir()
    assert safe_join(str(tmp_path), "chart") == str((tmp_path / "chart").resolve())


def test_safe_join_weigert_een_absoluut_pad(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="valt buiten"):
        safe_join(str(tmp_path), "/etc")


def test_safe_join_weigert_dubbele_punt(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="valt buiten"):
        safe_join(str(tmp_path), "chart/../..")


def test_safe_join_weigert_een_symlink_naar_buiten(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "buiten").mkdir()
    (root / "weg").symlink_to(tmp_path / "buiten")

    with pytest.raises(ValueError, match="valt buiten"):
        safe_join(str(root), "weg")


@pytest.mark.parametrize("value", ["", "/", "../common", "a/b", "a\\b", ".", "..", ".hidden"])
def test_single_segment_weigert(value: str) -> None:
    with pytest.raises(ValueError, match="'naam'"):
        single_segment(value, "naam")


def test_single_segment_laat_een_naam_door() -> None:
    assert single_segment("common", "naam") == "common"


def test_reject_escaping_symlinks_laat_een_interne_symlink_staan(tmp_path: Path) -> None:
    (tmp_path / "doel").write_text("ja")
    (tmp_path / "link").symlink_to(tmp_path / "doel")

    reject_escaping_symlinks(str(tmp_path))


def test_reject_escaping_symlinks_weigert_een_symlink_naar_buiten(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "geheim").write_text("nee")
    (root / "link").symlink_to(tmp_path / "geheim")

    with pytest.raises(ValueError, match="wijst buiten"):
        reject_escaping_symlinks(str(root))


def test_reject_escaping_symlinks_slaat_git_over(tmp_path: Path) -> None:
    root = tmp_path / "root"
    git_dir = root / ".git"
    git_dir.mkdir(parents=True)
    (tmp_path / "geheim").write_text("nee")
    (git_dir / "link").symlink_to(tmp_path / "geheim")

    reject_escaping_symlinks(str(root))


# --- helm-charts ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chart_dependency_buiten_de_clone_wordt_geweigerd(tmp_path: Path) -> None:
    """Een ``file://``-dependency uit de externe Chart.yaml mag niet uit de clone wijzen."""
    target = tmp_path / "deployment"
    target.mkdir()
    sentinel = tmp_path / "sentinel.txt"
    sentinel.write_text("onaangeroerd")

    def fill(root: Path) -> None:
        _chart(root, "name: app\ndependencies:\n  - name: common\n    repository: file://../../..\n")

    with (
        patch("opi.manager.project.helm_deployment.GitConnector", _fake_git_connector(fill)),
        pytest.raises(ValueError, match="buiten de gekloonde bron"),
    ):
        await clone_helm_chart(
            {"name": "app", "git-url": "https://example.test/app.git", "chart-path": "chart"},
            str(target),
        )

    assert sentinel.read_text() == "onaangeroerd"


@pytest.mark.asyncio
async def test_dependencynaam_met_een_scheidingsteken_wordt_geweigerd(tmp_path: Path) -> None:
    target = tmp_path / "deployment"
    target.mkdir()

    def fill(root: Path) -> None:
        _chart(root, "name: app\ndependencies:\n  - name: ../ontsnapt\n    repository: file://.\n")

    with (
        patch("opi.manager.project.helm_deployment.GitConnector", _fake_git_connector(fill)),
        pytest.raises(ValueError, match="één padsegment"),
    ):
        await clone_helm_chart(
            {"name": "app", "git-url": "https://example.test/app.git", "chart-path": "chart"},
            str(target),
        )


@pytest.mark.asyncio
async def test_chartnaam_met_een_scheidingsteken_wordt_geweigerd(tmp_path: Path) -> None:
    """``helm-charts[].name`` is de bestemmingsmap, dus één padsegment."""
    target = tmp_path / "deployment"
    target.mkdir()
    sentinel = tmp_path / "sentinel.txt"
    sentinel.write_text("onaangeroerd")

    with (
        patch(
            "opi.manager.project.helm_deployment.GitConnector",
            _fake_git_connector(lambda root: _chart(root, "name: app\n")),
        ),
        pytest.raises(ValueError, match="één padsegment"),
    ):
        await clone_helm_chart(
            {"name": "../../sentinel.txt", "git-url": "https://example.test/app.git", "chart-path": "chart"},
            str(target),
        )

    assert sentinel.read_text() == "onaangeroerd"


@pytest.mark.asyncio
async def test_chartpad_buiten_de_clone_wordt_geweigerd(tmp_path: Path) -> None:
    target = tmp_path / "deployment"
    target.mkdir()

    with (
        patch(
            "opi.manager.project.helm_deployment.GitConnector",
            _fake_git_connector(lambda root: _chart(root, "name: app\n")),
        ),
        pytest.raises(ValueError, match="buiten de gekloonde bron"),
    ):
        await clone_helm_chart(
            {"name": "app", "git-url": "https://example.test/app.git", "chart-path": "/etc"},
            str(target),
        )


@pytest.mark.asyncio
async def test_een_gewone_chart_wordt_nog_gekopieerd(tmp_path: Path) -> None:
    target = tmp_path / "deployment"
    target.mkdir()

    def fill(root: Path) -> None:
        _chart(root, "name: app\ndependencies:\n  - name: common\n    repository: file://./common\n")
        common = root / "chart" / "common"
        common.mkdir()
        (common / "Chart.yaml").write_text("name: common\n")

    with patch("opi.manager.project.helm_deployment.GitConnector", _fake_git_connector(fill)):
        dest = await clone_helm_chart(
            {"name": "app", "git-url": "https://example.test/app.git", "chart-path": "chart"},
            str(target),
        )

    assert Path(dest) == (target / "charts" / "app").resolve()
    assert (target / "charts" / "app" / "values.yaml").read_text() == "replicas: 1\n"
    assert (target / "charts" / "common" / "Chart.yaml").read_text() == "name: common\n"


@pytest.mark.asyncio
async def test_een_interne_symlink_in_de_chart_blijft_een_symlink(tmp_path: Path) -> None:
    """``copytree`` dereferenceert standaard symlinks; wat binnen de clone blijft moet als
    symlink overkomen in plaats van als kopie van de inhoud van zijn doel."""
    target = tmp_path / "deployment"
    target.mkdir()

    def fill(root: Path) -> None:
        _chart(root, "name: app\n")
        (root / "chart" / "values-link.yaml").symlink_to(root / "chart" / "values.yaml")

    with patch("opi.manager.project.helm_deployment.GitConnector", _fake_git_connector(fill)):
        await clone_helm_chart(
            {"name": "app", "git-url": "https://example.test/app.git", "chart-path": "chart"},
            str(target),
        )

    assert (target / "charts" / "app" / "values-link.yaml").is_symlink()


@pytest.mark.asyncio
async def test_een_symlink_naar_buiten_in_de_chart_wordt_geweigerd(tmp_path: Path) -> None:
    target = tmp_path / "deployment"
    target.mkdir()

    def fill(root: Path) -> None:
        _chart(root, "name: app\n")
        (root / "geheim.txt").write_text("nee")
        (root / "chart" / "link.txt").symlink_to(root / "geheim.txt")

    with (
        patch("opi.manager.project.helm_deployment.GitConnector", _fake_git_connector(fill)),
        pytest.raises(ValueError, match="wijst buiten"),
    ):
        await clone_helm_chart(
            {"name": "app", "git-url": "https://example.test/app.git", "chart-path": "chart"},
            str(target),
        )

    assert not (target / "charts" / "app").exists()


# --- helmfile -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_helmfilepad_buiten_de_clone_wordt_geweigerd(tmp_path: Path) -> None:
    target = tmp_path / "deployment"

    fake = _fake_git_connector(lambda root: (root / "helmfile.yaml").write_text("releases: []\n"))
    with (
        patch("opi.manager.project.helm_deployment.GitConnector", fake),
        pytest.raises(ValueError, match="buiten de gekloonde bron"),
    ):
        await clone_helmfile_source(
            {"name": "docs", "url": "https://example.test/infra.git", "path": "../../etc"},
            str(target),
        )

    assert not target.exists()


@pytest.mark.asyncio
async def test_helmfile_entry_buiten_het_pad_wordt_geweigerd(tmp_path: Path) -> None:
    target = tmp_path / "deployment"

    fake = _fake_git_connector(lambda root: (root / "helmfile.yaml").write_text("releases: []\n"))
    with (
        patch("opi.manager.project.helm_deployment.GitConnector", fake),
        pytest.raises(ValueError, match="buiten de gekloonde bron"),
    ):
        await clone_helmfile_source(
            {"name": "docs", "url": "https://example.test/infra.git", "entry": "/etc"},
            str(target),
        )


@pytest.mark.asyncio
async def test_symlink_naar_buiten_in_de_helmfilebron_wordt_geweigerd(tmp_path: Path) -> None:
    target = tmp_path / "deployment"

    def fill(root: Path) -> None:
        (root / "helmfile.yaml").write_text("releases: []\n")
        (root / "link.txt").symlink_to(tmp_path / "geheim.txt")

    (tmp_path / "geheim.txt").write_text("nee")

    with (
        patch("opi.manager.project.helm_deployment.GitConnector", _fake_git_connector(fill)),
        pytest.raises(ValueError, match="wijst buiten"),
    ):
        await clone_helmfile_source(
            {"name": "docs", "url": "https://example.test/infra.git"},
            str(target),
        )

    assert not target.exists()


@pytest.mark.asyncio
async def test_een_gewone_helmfilebron_wordt_nog_gekopieerd(tmp_path: Path) -> None:
    target = tmp_path / "deployment"

    def fill(root: Path) -> None:
        (root / "helmfile.yaml").write_text("releases: []\n")
        (root / "environments").mkdir()
        (root / "environments" / "prd.yaml").write_text("a: 1\n")

    with patch("opi.manager.project.helm_deployment.GitConnector", _fake_git_connector(fill)):
        dest, entry = await clone_helmfile_source(
            {"name": "docs", "url": "https://example.test/infra.git", "path": ""},
            str(target),
        )

    assert (Path(dest), entry) == (target, "")
    assert (target / "environments" / "prd.yaml").read_text() == "a: 1\n"


@pytest.mark.asyncio
async def test_een_interne_symlink_in_de_helmfilebron_blijft_een_symlink(tmp_path: Path) -> None:
    target = tmp_path / "deployment"

    def fill(root: Path) -> None:
        (root / "helmfile.yaml").write_text("releases: []\n")
        (root / "helmfile-link.yaml").symlink_to(root / "helmfile.yaml")

    with patch("opi.manager.project.helm_deployment.GitConnector", _fake_git_connector(fill)):
        await clone_helmfile_source(
            {"name": "docs", "url": "https://example.test/infra.git"},
            str(target),
        )

    assert (target / "helmfile-link.yaml").is_symlink()


@pytest.mark.asyncio
async def test_een_symlink_naar_een_map_in_de_helmfilebron_blijft_een_symlink(tmp_path: Path) -> None:
    target = tmp_path / "deployment"

    def fill(root: Path) -> None:
        (root / "helmfile.yaml").write_text("releases: []\n")
        (root / "environments").mkdir()
        (root / "environments" / "prd.yaml").write_text("a: 1\n")
        (root / "env-link").symlink_to("environments", target_is_directory=True)

    with patch("opi.manager.project.helm_deployment.GitConnector", _fake_git_connector(fill)):
        await clone_helmfile_source(
            {"name": "docs", "url": "https://example.test/infra.git"},
            str(target),
        )

    assert (target / "env-link").is_symlink()
    assert os.readlink(target / "env-link") == "environments"


async def _twee_keer_clonen(fill: Callable[[Path], None], target: Path) -> None:
    """Twee uitrollen van dezelfde bron naar hetzelfde target_path."""
    with patch("opi.manager.project.helm_deployment.GitConnector", _fake_git_connector(fill)):
        for _ in range(2):
            await clone_helmfile_source(
                {"name": "docs", "url": "https://example.test/infra.git"},
                str(target),
            )


@pytest.mark.asyncio
async def test_een_tweede_uitrol_overschrijft_een_losse_symlink(tmp_path: Path) -> None:
    def fill(root: Path) -> None:
        (root / "helmfile.yaml").write_text("releases: []\n")
        (root / "helmfile-link.yaml").symlink_to(root / "helmfile.yaml")

    target = tmp_path / "deployment"
    await _twee_keer_clonen(fill, target)

    assert (target / "helmfile-link.yaml").is_symlink()


@pytest.mark.asyncio
async def test_een_tweede_uitrol_overschrijft_een_geneste_symlink(tmp_path: Path) -> None:
    def fill(root: Path) -> None:
        (root / "helmfile.yaml").write_text("releases: []\n")
        sub = root / "sub"
        sub.mkdir()
        (sub / "a.yaml").write_text("a: 1\n")
        (sub / "a-link.yaml").symlink_to(sub / "a.yaml")

    target = tmp_path / "deployment"
    await _twee_keer_clonen(fill, target)

    assert (target / "sub" / "a-link.yaml").is_symlink()


@pytest.mark.asyncio
async def test_een_tweede_uitrol_zonder_symlink_blijft_groen(tmp_path: Path) -> None:
    def fill(root: Path) -> None:
        (root / "helmfile.yaml").write_text("releases: []\n")
        (root / "environments").mkdir()
        (root / "environments" / "prd.yaml").write_text("a: 1\n")

    target = tmp_path / "deployment"
    await _twee_keer_clonen(fill, target)

    assert (target / "environments" / "prd.yaml").read_text() == "a: 1\n"


# --- schema ---------------------------------------------------------------------------


def _project(**extra: Any) -> dict[str, Any]:
    return {
        "name": "helm-project",
        "users": [{"email": "admin@rijksoverheid.nl", "roles": ["admin", "ops"]}],
        **extra,
    }


@pytest.mark.parametrize(
    "helm_chart",
    [
        {"name": "../ontsnapt"},
        {"name": "App"},
        {"name": "app", "chart-path": "/etc/passwd"},
        {"name": "app", "chart-path": "../../etc"},
        {"name": "app", "chart-path": "charts\\common"},
        {"name": "app", "git-url": "file:///etc"},
    ],
)
def test_schema_weigert_een_helm_chart(helm_chart: dict[str, Any]) -> None:
    with pytest.raises(ProjectSchemaError):
        validate_project_schema(_project(**{"helm-charts": [helm_chart]}))


def test_schema_laat_een_gewone_helm_chart_door() -> None:
    validate_project_schema(
        _project(
            **{
                "helm-charts": [
                    {
                        "name": "app",
                        "git-url": "https://example.test/app.git",
                        "git-ref": "main",
                        "chart-path": "charts/app",
                    }
                ]
            }
        )
    )


@pytest.mark.parametrize(
    "helmfile",
    [
        {"name": "../ontsnapt", "url": "https://example.test/infra.git"},
        {"name": "docs", "url": "https://example.test/infra.git", "path": "/etc"},
        {"name": "docs", "url": "https://example.test/infra.git", "path": "a/../b"},
        {"name": "docs", "url": "https://example.test/infra.git", "entry": "../../etc"},
    ],
)
def test_schema_weigert_een_helmfile(helmfile: dict[str, Any]) -> None:
    with pytest.raises(ProjectSchemaError):
        validate_project_schema(_project(helmfile=[helmfile]))


def test_schema_laat_de_bestaande_helmfilevorm_door() -> None:
    """De vorm van de twee productieprojecten (``path: ''``), plus een ``entry`` zoals de
    code hem al leest; die was in het schema nog niet toegestaan."""
    validate_project_schema(
        _project(helmfile=[{"name": "docs", "url": "https://example.test/infra.git", "ref": "main", "path": ""}])
    )
    validate_project_schema(
        _project(helmfile=[{"name": "grist", "url": "https://example.test/infra.git", "entry": "helmfiles/grist"}])
    )


def test_omgeving_heeft_geen_invloed_op_de_helper(tmp_path: Path) -> None:
    """De toets kijkt naar het pad, niet naar de werkmap van het proces."""
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        with pytest.raises(ValueError, match="valt buiten"):
            safe_join(str(tmp_path / "root"), "../elders")
    finally:
        os.chdir(cwd)
