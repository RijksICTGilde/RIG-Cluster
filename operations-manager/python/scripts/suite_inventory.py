"""Inventory of the unit suite: per test file the count, the duration, the kind and the module.

Reads a junit-xml written by pytest (`task test-inventaris`) and prints a markdown report.
The kind is derived from what a file uses, so the report stays correct when files move.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

TESTS_ROOT = Path(__file__).resolve().parents[1] / "tests"

#: The kinds, in the order they win: a file that starts a real database is a real-database
#: file even if it also renders a template.
KINDS: list[tuple[str, str]] = [
    ("real_database", "echte database"),
    ("real_binary", "echte binary"),
    ("http_client", "HTTP-client tegen de app"),
    ("static_code", "statische toets op de code"),
    ("render", "template of HTML renderen"),
    ("mock_interaction", "mock-interactie"),
    ("own_logic", "eigen logica zonder IO"),
]

_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "real_database": (re.compile(r"\b(orm_db|migration_db|_orm_db_url|postgres_container)\b"),),
    "real_binary": (
        re.compile(r"\b(sops|age_keygen|age-keygen|kustomize|git_http_remote|docker_runtime)\b"),
        re.compile(r"shutil\.which\(|subprocess\.(run|check_output|Popen)"),
    ),
    "http_client": (re.compile(r"\b(TestClient|test_client|async_client|httpx\.AsyncClient)\b"),),
    "static_code": (
        re.compile(r"\bast\.(parse|walk)\b"),
        re.compile(r"REPO_ROOT|PROJECT_ROOT|parents\[\d+\]\s*/\s*['\"]\.github"),
    ),
    "render": (re.compile(r"\b(render_template|get_template|from_string|jinja|TemplateRenderer)\b", re.IGNORECASE),),
}

_ASSERT_CALLED = re.compile(r"assert_(?:any_|not_|)call(?:ed\w*|s\w*)?\(")
_ASSERT = re.compile(r"^\s*assert\b", re.MULTILINE)
_OPI_IMPORT = re.compile(r"^\s*from (opi(?:\.[\w.]+)?) import|^\s*import (opi(?:\.[\w.]+)?)", re.MULTILINE)


@dataclass
class FileStats:
    path: str
    tests: int = 0
    seconds: float = 0.0
    failures: int = 0
    errors: int = 0
    skipped: int = 0
    slowest: list[tuple[float, str]] = field(default_factory=list)


def classify(source: str) -> str:
    for kind, _ in KINDS:
        for pattern in _PATTERNS.get(kind, ()):
            if pattern.search(source):
                return kind
    asserts = len(_ASSERT.findall(source))
    called = len(_ASSERT_CALLED.findall(source))
    if called and called * 2 >= max(asserts, 1):
        return "mock_interaction"
    return "own_logic"


def module_of(source: str) -> str:
    modules = Counter()
    for first, second in _OPI_IMPORT.findall(source):
        name = first or second
        parts = name.split(".")
        modules[".".join(parts[:3]) if len(parts) >= 3 else name] += 1
    return modules.most_common(1)[0][0] if modules else "-"


def file_of(case_file: str | None, classname: str) -> str:
    """The test file of a testcase: junit only names the dotted class, so walk it back."""
    if case_file:
        return case_file
    parts = classname.split(".")
    while parts:
        candidate = "/".join(parts) + ".py"
        if (TESTS_ROOT.parent / candidate).is_file():
            return candidate
        parts.pop()
    return classname.replace(".", "/") + ".py"


def read_junit(xml_path: Path) -> dict[str, FileStats]:
    per_file: dict[str, FileStats] = {}
    for case in ET.parse(xml_path).iter("testcase"):  # noqa: S314 (onze eigen pytest-uitvoer)
        path = file_of(case.get("file"), case.get("classname") or "")
        stats = per_file.setdefault(path, FileStats(path))
        stats.tests += 1
        seconds = float(case.get("time") or 0.0)
        stats.seconds += seconds
        stats.slowest.append((seconds, case.get("name") or "?"))
        stats.failures += len(case.findall("failure"))
        stats.errors += len(case.findall("error"))
        stats.skipped += len(case.findall("skipped"))
    return per_file


def source_of(path: str) -> str:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = TESTS_ROOT.parent / path
    try:
        return candidate.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def report(per_file: dict[str, FileStats], slowest_files: int, slowest_tests: int) -> str:
    kinds: dict[str, list[FileStats]] = defaultdict(list)
    modules: dict[str, FileStats] = {}
    for path, stats in per_file.items():
        source = source_of(path)
        kinds[classify(source)].append(stats)
        modules[path] = module_of(source)

    total_tests = sum(s.tests for s in per_file.values())
    total_seconds = sum(s.seconds for s in per_file.values())
    lines = [
        "# Inventaris van de unit-suite",
        "",
        f"{len(per_file)} bestanden, {total_tests} toetsen, {total_seconds / 60:.1f} minuten aan toetstijd.",
        "",
        "## Verdeling over de soorten",
        "",
        "| Soort | Bestanden | Toetsen | Minuten | % van de tijd |",
        "|---|---:|---:|---:|---:|",
    ]
    for kind, label in KINDS:
        group = kinds.get(kind, [])
        seconds = sum(s.seconds for s in group)
        share = 100 * seconds / total_seconds if total_seconds else 0
        lines.append(f"| {label} | {len(group)} | {sum(s.tests for s in group)} | {seconds / 60:.1f} | {share:.1f}% |")

    lines += [
        "",
        f"## De traagste {slowest_files} bestanden",
        "",
        "| Bestand | Toetsen | Seconden | Soort | Module |",
        "|---|---:|---:|---|---|",
    ]
    labels = dict(KINDS)
    by_time = sorted(per_file.values(), key=lambda s: s.seconds, reverse=True)
    for stats in by_time[:slowest_files]:
        kind = next(k for k, group in kinds.items() if stats in group)
        lines.append(
            f"| `{stats.path}` | {stats.tests} | {stats.seconds:.1f} | {labels[kind]} | `{modules[stats.path]}` |"
        )

    lines += ["", f"## De traagste {slowest_tests} losse toetsen", "", "| Toets | Seconden |", "|---|---:|"]
    everything = [(seconds, f"{stats.path}::{name}") for stats in per_file.values() for seconds, name in stats.slowest]
    for seconds, nodeid in sorted(everything, reverse=True)[:slowest_tests]:
        lines.append(f"| `{nodeid}` | {seconds:.1f} |")

    broken = [s for s in by_time if s.failures or s.errors]
    if broken:
        lines += [
            "",
            "## Bestanden met rood in deze run",
            "",
            "| Bestand | Rood | Fout | Overgeslagen |",
            "|---|---:|---:|---:|",
        ]
        for stats in broken:
            lines.append(f"| `{stats.path}` | {stats.failures} | {stats.errors} | {stats.skipped} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("junit_xml", type=Path, help="de junit-xml van de run")
    parser.add_argument(
        "--above",
        type=float,
        help="print alleen de bestanden die meer dan dit aantal seconden kosten, een per regel",
    )
    parser.add_argument("--slowest-files", type=int, default=50)
    parser.add_argument("--slowest-tests", type=int, default=30)
    parser.add_argument("--output", type=Path, help="schrijf naar dit bestand in plaats van stdout")
    args = parser.parse_args()

    if not args.junit_xml.exists():
        print(f"geen junit-xml op {args.junit_xml}", file=sys.stderr)
        return 1
    per_file = read_junit(args.junit_xml)
    if args.above is not None:
        for stats in sorted(per_file.values(), key=lambda s: -s.seconds):
            if stats.seconds > args.above:
                print(stats.path)
        return 0
    text = report(per_file, args.slowest_files, args.slowest_tests)
    if args.output:
        args.output.write_text(text, encoding="utf-8")
        print(f"overzicht in {args.output}")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
