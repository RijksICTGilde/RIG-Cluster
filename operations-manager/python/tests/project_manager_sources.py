"""The sources of ``ProjectManager``: the module itself plus the groups split out of it.

Source guards that read only ``project_manager.py`` go quiet the moment the code they watch
moves into ``opi/manager/project/``: the file they parse no longer contains it and the
assertion passes on nothing. They read through here instead, so a move keeps them honest.
"""

from __future__ import annotations

import ast
from pathlib import Path

from opi.manager import project_manager as project_manager_module

PROJECT_MANAGER_PATH = Path(project_manager_module.__file__)
PROJECT_PACKAGE_DIR = PROJECT_MANAGER_PATH.parent / "project"


def source_paths() -> list[Path]:
    """``project_manager.py`` plus every module under ``opi/manager/project/``."""
    return [PROJECT_MANAGER_PATH, *sorted(PROJECT_PACKAGE_DIR.rglob("*.py"))]


def sources() -> list[tuple[Path, str]]:
    """Each source path with its text."""
    return [(path, path.read_text(encoding="utf-8")) for path in source_paths()]


def source_text() -> str:
    """All sources as one text, for guards that count or search strings."""
    return "\n".join(text for _, text in sources())


def trees() -> list[tuple[Path, ast.Module]]:
    """Each source path with its parsed module."""
    return [(path, ast.parse(text, filename=str(path))) for path, text in sources()]


def defined_in_project_manager() -> set[str]:
    """The module-level names ``project_manager.py`` defines itself."""
    names: set[str] = set()
    for node in ast.parse(PROJECT_MANAGER_PATH.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(target.id for target in node.targets if isinstance(target, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def borrowed_by_project_manager() -> dict[str, str]:
    """The names ``project_manager.py`` imports, mapped to the module they come from."""
    return {
        alias.asname or alias.name: node.module or "?"
        for node in ast.walk(ast.parse(PROJECT_MANAGER_PATH.read_text(encoding="utf-8")))
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
