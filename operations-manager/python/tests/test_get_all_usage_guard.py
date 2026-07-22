"""Guard: ``ProjectStore.get_all()`` returns a list, so it must not be used as a map.

``get_all_projects()`` used to return ``dict[str, Project]``. ``get_all()`` returns
``list[Project]``. Four call sites survived that change unnoticed because neither
mistake raises:

    if name not in projects:   # list membership vs a str -> silently always True
    projects[name]             # only reached when the guard above is wrong
    for name in projects:      # yields Project objects, not names

pyright cannot back this up: reportIndexIssue, reportArgumentType and
reportCallIssue are all disabled in pyproject.toml. So this AST guard exists to
make the mistake impossible to reintroduce rather than merely forbidden.

Use ``store.get(name)`` for a single project -- the cache is keyed by name, so it
is an O(1) lookup and does not materialise the whole collection.
"""

from __future__ import annotations

import ast
from pathlib import Path

OPI_ROOT = Path(__file__).resolve().parent.parent / "opi"


def _get_all_variables(tree: ast.AST) -> dict[str, list[str]]:
    """Map function name -> variables assigned from a ``.get_all()`` call."""
    found: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        names: list[str] = []
        for stmt in ast.walk(node):
            if not isinstance(stmt, ast.Assign):
                continue
            value = stmt.value
            if (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Attribute)
                and value.func.attr == "get_all"
                and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
            ):
                names.append(stmt.targets[0].id)
        if names:
            found[node.name] = names
    return found


def _violations_in(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    problems: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        tracked = _get_all_variables(ast.Module(body=[node], type_ignores=[])).get(node.name, [])
        if not tracked:
            continue

        for stmt in ast.walk(node):
            # `name in projects` / `name not in projects`
            if isinstance(stmt, ast.Compare):
                for op, comparator in zip(stmt.ops, stmt.comparators, strict=False):
                    if (
                        isinstance(op, ast.In | ast.NotIn)
                        and isinstance(comparator, ast.Name)
                        and comparator.id in tracked
                    ):
                        problems.append(
                            f"{path.name}:{stmt.lineno}: membership test against '{comparator.id}' "
                            f"(get_all() is a list; use store.get(name) instead)"
                        )
            # `projects[name]` with a non-integer key
            if (
                isinstance(stmt, ast.Subscript)
                and isinstance(stmt.value, ast.Name)
                and stmt.value.id in tracked
                and not isinstance(stmt.slice, ast.Constant | ast.Slice)
            ):
                problems.append(
                    f"{path.name}:{stmt.lineno}: subscript of '{stmt.value.id}' by name "
                    f"(get_all() is a list; use store.get(name) instead)"
                )
            # `for name in projects:` where the loop variable is then used as a string
            if (
                isinstance(stmt, ast.comprehension | ast.For)
                and isinstance(stmt.iter, ast.Name)
                and stmt.iter.id in tracked
                and isinstance(stmt.target, ast.Name)
                and stmt.target.id in {"name", "project_name", "naam"}
            ):
                # ast.comprehension carries no lineno; its target Name does.
                lineno = getattr(stmt, "lineno", stmt.target.lineno)
                problems.append(
                    f"{path.name}:{lineno}: iterating '{stmt.iter.id}' into '{stmt.target.id}' "
                    f"(get_all() yields Project objects, not names; use project.name)"
                )

    return problems


def test_get_all_result_is_never_used_as_a_mapping() -> None:
    """No module may treat a get_all() result as if it were keyed by project name."""
    violations: list[str] = []
    for path in sorted(OPI_ROOT.rglob("*.py")):
        violations.extend(_violations_in(path))

    assert not violations, "get_all() misused as a mapping:\n" + "\n".join(violations)


def test_guard_detects_the_original_regression() -> None:
    """The guard must actually fire on the shape that shipped, or it guards nothing."""
    source = """
def handler(project_name):
    all_projects = get_project_store().get_all()
    if project_name not in all_projects:
        raise HTTPException(status_code=404)
    return all_projects[project_name]
"""
    tree = ast.parse(source)
    tracked = _get_all_variables(tree)
    assert tracked == {"handler": ["all_projects"]}

    tmp = Path(__file__).parent / "_guard_probe.py"
    tmp.write_text(source, encoding="utf-8")
    try:
        problems = _violations_in(tmp)
    finally:
        tmp.unlink()

    assert any("membership test" in p for p in problems), problems
    assert any("subscript" in p for p in problems), problems


def test_guard_detects_the_namespace_repr_regression() -> None:
    """And on the silent repr-interpolation shape from router_usage.py."""
    source = """
def namespaces(prefix):
    projects = get_project_store().get_all()
    return sorted({f"{prefix}{name}" for name in projects})
"""
    tmp = Path(__file__).parent / "_guard_probe_ns.py"
    tmp.write_text(source, encoding="utf-8")
    try:
        problems = _violations_in(tmp)
    finally:
        tmp.unlink()

    assert any("yields Project objects" in p for p in problems), problems
