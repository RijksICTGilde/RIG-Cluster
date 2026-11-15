"""Importing opi.forms must not depend on the load order.

An ``opi/forms/__init__.py`` that re-exports its submodules makes any ``opi.forms.*`` import
run ``opi.forms.visualizers.providers``, which reads
``opi.services.catalog.image_registries``, whose modules read ``opi.forms`` back. That cycle
resolves only when the order a process imports them in comes out right, and it makes a single
``import opi.forms.layout`` pull in the whole application, config and logging included.
"""

from __future__ import annotations

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

PYTHON_ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ("opi/forms", "opi/services/catalog/image_registries")

#: A fresh interpreter needs the project on its path; the rest of the environment is this
#: process's, so settings load the same way they do in a test.
CHILD_ENV = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}


def modules_of(package: str) -> list[str]:
    """Every module in a package, as a dotted name."""
    root = PYTHON_ROOT / package
    names = []
    for path in sorted(root.rglob("*.py")):
        parts = [part for part in path.relative_to(root).with_suffix("").parts if part != "__init__"]
        names.append(".".join([package.replace("/", "."), *parts]))
    return names


def import_in_a_fresh_process(module: str) -> subprocess.CompletedProcess[str]:
    """Import one module as the first thing a new interpreter does."""
    return subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        cwd=PYTHON_ROOT,
        env=CHILD_ENV,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_importing_the_forms_package_leaves_the_service_catalog_alone() -> None:
    """The arrow that made the cycle: the package itself must not reach the services."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import opi.forms, sys; print(any(m.startswith('opi.services') for m in sys.modules))",
        ],
        cwd=PYTHON_ROOT,
        env=CHILD_ENV,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == "False"


@pytest.mark.wide
@pytest.mark.parametrize("package", PACKAGES)
def test_every_module_imports_first_in_a_fresh_process(package: str) -> None:
    """A fresh interpreter per module, so no module borrows another one's load order."""
    modules = modules_of(package)
    assert modules
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(import_in_a_fresh_process, modules))
    failed = [
        f"{module}: {result.stderr.strip().splitlines()[-1]}"
        for module, result in zip(modules, results, strict=True)
        if result.returncode != 0
    ]
    assert not failed, "\n".join(failed)
