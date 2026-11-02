"""``helmfile[].files`` mag geen bestandsnaam gebruiken die de renderketen zelf kent.

De plugin leest ``kustomization.yaml``, ``decrypt-sops.yaml``, ``.cmp-env`` en
``.helmfile-entry`` uit de deploymentmap, en OPI schrijft ze daar. Een helmfile-bron die
zo'n naam meebrengt, bepaalt dus wat er gerenderd wordt in plaats van alleen wat er bij
komt. Eerder werd een rare naam alleen overgeslagen met een waarschuwing in de logs, dus
de gebruiker merkte niets.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from opi.manager.project_manager import HELMFILE_RESERVED_FILENAMES, ProjectManager

if TYPE_CHECKING:
    from pathlib import Path


def _write(tmp_path: Path, filename: str) -> list[str]:
    manager = MagicMock(spec=ProjectManager)
    return ProjectManager._write_helmfile_custom_files(manager, {"files": {filename: "inhoud"}}, {}, str(tmp_path))


@pytest.mark.parametrize("filename", sorted(HELMFILE_RESERVED_FILENAMES))
def test_a_reserved_name_is_refused(tmp_path: Path, filename: str) -> None:
    with pytest.raises(ValueError, match=filename.replace(".", r"\.")):
        _write(tmp_path, filename)


@pytest.mark.parametrize("filename", sorted(HELMFILE_RESERVED_FILENAMES))
def test_a_reserved_name_in_a_subfolder_is_refused(tmp_path: Path, filename: str) -> None:
    with pytest.raises(ValueError, match=filename.replace(".", r"\.")):
        _write(tmp_path, f"sub/{filename}")


def test_an_ordinary_name_is_still_written(tmp_path: Path) -> None:
    assert _write(tmp_path, "sub/values-extra.yaml") == ["sub/values-extra.yaml"]
    assert (tmp_path / "sub/values-extra.yaml").read_text() == "inhoud"
