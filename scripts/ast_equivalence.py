"""Compare the AST of a Python file between two commits, with docstrings left out.

    python3 scripts/ast_equivalence.py --base <ref> operations-manager/python/opi/utils/qr.py
    python3 scripts/ast_equivalence.py --base <ref> --rename oud=new <path>...

This is the proof that a cleanup round changed text and nothing else. Comments are not in the
AST and docstrings are stripped here, so a file whose comments and docstrings were rewritten
comes out SAME. A changed string literal, a moved statement or a changed name comes out DIFFER.

``--rename old=new`` renames identifiers in the BASE tree before comparing, so a module whose
Dutch names were translated can still be shown to be otherwise untouched: what the table does
not cover shows up as a difference.

A ref is anything ``git show`` accepts; ``--head`` defaults to the working tree.
"""

from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

_DEFINITION = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)

#: Where an identifier lives per node type. ``Global`` and ``Nonlocal`` hold a list of plain
#: strings instead and are handled apart.
_NAME_FIELDS: dict[type[ast.AST], tuple[str, ...]] = {
    ast.Name: ("id",),
    ast.arg: ("arg",),
    ast.FunctionDef: ("name",),
    ast.AsyncFunctionDef: ("name",),
    ast.ClassDef: ("name",),
    ast.Attribute: ("attr",),
    ast.keyword: ("arg",),
    ast.ExceptHandler: ("name",),
    ast.alias: ("name", "asname"),
}


def strip_docstrings(tree: ast.Module) -> ast.Module:
    """Drop the leading string expression of every module, class and function.

    A definition left without a body gets a ``pass``, so a function that was only a docstring
    compares equal to one that is only a ``pass``: both do nothing.
    """
    for node in ast.walk(tree):
        if not isinstance(node, _DEFINITION):
            continue
        first = node.body[0] if node.body else None
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
            node.body = node.body[1:] or [ast.Pass()]
    return tree


def apply_renames(tree: ast.Module, renames: dict[str, str]) -> ast.Module:
    for node in ast.walk(tree):
        for field in _NAME_FIELDS.get(type(node), ()):
            current = getattr(node, field, None)
            if isinstance(current, str) and current in renames:
                setattr(node, field, renames[current])
        if isinstance(node, ast.Global | ast.Nonlocal):
            node.names = [renames.get(name, name) for name in node.names]
    return tree


def normalised(source: str, renames: dict[str, str] | None = None) -> ast.Module:
    tree = strip_docstrings(ast.parse(source))
    return apply_renames(tree, renames) if renames else tree


def _label(statement: ast.stmt) -> str | None:
    if isinstance(statement, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
        return statement.name
    return None


def _first_difference(base: list[ast.stmt], head: list[ast.stmt], scope: str) -> str:
    """Where two bodies first part ways, named after the definition it happened in."""
    for index, (left, right) in enumerate(zip(base, head, strict=False)):
        if ast.dump(left) == ast.dump(right):
            continue
        name = _label(left)
        if name and name == _label(right):
            return _first_difference(left.body, right.body, f"{scope}{name}.")  # type: ignore[attr-defined]
        return f"{scope}statement {index + 1} ({type(left).__name__} vs {type(right).__name__})"
    return f"{scope}body is {len(base)} statements before and {len(head)} after"


def difference(base_source: str, head_source: str, renames: dict[str, str] | None = None) -> str | None:
    """What changed beyond comments, docstrings and the renames, or ``None`` when nothing did."""
    base = normalised(base_source, renames)
    head = normalised(head_source)
    if ast.dump(base) == ast.dump(head):
        return None
    return _first_difference(base.body, head.body, "")


def read_at(ref: str | None, path: Path) -> str:
    if ref is None:
        return path.read_text(encoding="utf-8")
    relative = path.resolve().relative_to(REPO).as_posix()
    completed = subprocess.run(
        ["git", "-C", str(REPO), "show", f"{ref}:{relative}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout


def parse_renames(pairs: list[str]) -> dict[str, str]:
    renames: dict[str, str] = {}
    for pair in pairs:
        old, separator, new = pair.partition("=")
        if not separator or not old or not new:
            raise SystemExit(f"--rename wants old=new, got {pair!r}")
        renames[old] = new
    return renames


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", required=True, help="the ref to compare against")
    parser.add_argument("--head", default=None, help="the other side; the working tree when left out")
    parser.add_argument("--rename", action="append", default=[], metavar="OLD=NEW", help="rename in the base tree")
    parser.add_argument("paths", nargs="+", type=Path, help="the files to compare")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    renames = parse_renames(arguments.rename)

    differing = 0
    for path in arguments.paths:
        detail = difference(read_at(arguments.base, path), read_at(arguments.head, path), renames)
        if detail is None:
            print(f"SAME   {path}")
        else:
            differing += 1
            print(f"DIFFER {path}: {detail}")

    print(f"\n{len(arguments.paths)} files, {differing} differing")
    return 1 if differing else 0


if __name__ == "__main__":
    sys.exit(main())
