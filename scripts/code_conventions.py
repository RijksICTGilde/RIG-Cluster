"""Measure the conventions from ``docs/development/coding-rules.md`` that ruff cannot see.

Three counts per Python file:

1. **Dutch identifiers.** Code is English (chapter 2). A module, class, function, parameter or
   variable name whose parts hold a Dutch word counts.
2. **Dutch comments and docstrings.** Same rule. Other strings are NOT measured: the text a user
   sees may be Dutch, so a label, a ``help_text`` or an ``HTTPException`` detail is left alone.
3. **Functions with more comment than code.** Chapter 6: a function with more ``#`` lines than
   code lines explains something the code should have said itself.

Recognising Dutch is a word list and nothing more. It holds only words that cannot be read as
English, and a word is left out when it can be both, so the counts are an undercount: that is
the direction a ratchet can carry, because a wrong word produces an alarm nobody can act on.

``check-code-conventions.py`` is the entry point over this module: it parses the flags and
returns the exit code.
"""

from __future__ import annotations

import ast
import io
import json
import re
import tokenize
from dataclasses import dataclass
from pathlib import Path

#: Words that read as Dutch in prose as well as in a name. Every one of them is absent from
#: English, so a single hit is enough. Deliberately NOT here: "met" (English past tense of meet),
#: "op" ("no-op"), "na" ("N/A"), "die", "had", "want", "al" ("et al."), "om" (the OPI shorthand),
#: "basis", "pad", "lid" and "start", which all carry an English reading.
_DUTCH_CORE = frozenset(
    """
    een het niet niets geen wordt worden werd moet moeten deze dit dat zijn naar ook bij wanneer
    maar omdat zodat dus alleen altijd nooit eerst eerste tweede derde daarna hier waar hoe wat
    wie welke zoals zonder tegen tussen onder boven achter voor aan uit als nog wel toch veel
    weinig minder beter heeft hebben kan kunnen kon mag mogen zal zullen zou gaat gaan komt komen
    staat staan blijft blijven geeft geven maakt maken maak haal haalt zet zetten controleer
    controleert verwijder verwijdert toets toetst krijgt laat houdt noemt draagt weigert weigeren
    verwacht gevonden geweigerd twee drie elke elk ander andere dezelfde eigen nieuwe nieuw oude
    lege leeg buiten binnen terug mee samen soms vaak reden soort vorm stap stappen dienst
    diensten sleutel sleutels waarde waardes waarden naam namen veld velden bestand bestanden
    pagina adres taak taken melding meldingen lijst verzoek sessie poort antwoord resultaat tekst
    regel regels aantal bron fout fouten blok rij rijen rol rollen weg leden voortgang bewaker
    bewakers toestand gebruiker gebruikers wijziging wijzigingen ronde rondes tak takken grens
    grenzen uitvoer invoer teller telling keuze doel doelen inhoud oorzaak volgorde uitkomst
    vraag vragen hele zelf iets elkaar je hij wij zij er te van
    """.split()  # noqa: SIM905  # 200 words as a list literal is a wall; this reads as the list it is
)

#: Dutch in a name, but not reliable in prose: "de facto" and "de-duplicate" are English.
DUTCH_IN_IDENTIFIERS = _DUTCH_CORE | frozenset({"de"})
DUTCH_IN_PROSE = _DUTCH_CORE

#: Proper nouns and domain terms that never count, whatever a word list says about their parts.
ALLOWLIST = frozenset({"zad", "rijksapp", "rijksapps", "rijksoverheid", "odcn", "wies", "opi", "rig"})

DUTCH_IDENTIFIERS = "dutch-identifiers"
DUTCH_COMMENTS = "dutch-comments"
COMMENT_HEAVY = "comment-heavy-functions"
MEASURES = (DUTCH_IDENTIFIERS, DUTCH_COMMENTS, COMMENT_HEAVY)

_NON_LETTERS = re.compile(r"[^A-Za-z]+")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
#: Quoted spans are left out of prose: an English docstring may quote a Dutch UI string or an
#: example database name. Single quotes are deliberately not here, because an apostrophe would
#: pair up with the next one and swallow the prose between them. A run of backticks matches a
#: run, so the Sphinx form ``code`` is a span like `code` is.
_QUOTED = re.compile(r'"[^"\n]*"|`+[^`\n]*`+')


@dataclass(frozen=True)
class Finding:
    """One line that counts, with the word or ratio that made it count."""

    path: str
    measure: str
    line_number: int
    detail: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line_number}: {self.measure} ({self.detail})"


def identifier_parts(name: str) -> list[str]:
    """``renderPaginaNaam`` and ``render_pagina_naam`` both come apart into the same words."""
    parts: list[str] = []
    for chunk in _NON_LETTERS.split(name):
        parts.extend(piece.lower() for piece in _CAMEL_BOUNDARY.split(chunk) if piece)
    return parts


def dutch_in_identifier(name: str) -> str | None:
    for part in identifier_parts(name):
        if part not in ALLOWLIST and part in DUTCH_IN_IDENTIFIERS:
            return part
    return None


def dutch_in_prose(text: str) -> str | None:
    for word in _NON_LETTERS.split(_QUOTED.sub(" ", text).lower()):
        if word and word not in ALLOWLIST and word in DUTCH_IN_PROSE:
            return word
    return None


@dataclass(frozen=True)
class _Parsed:
    """What one walk over the tree yields, for all three measures."""

    functions: list[ast.FunctionDef | ast.AsyncFunctionDef]
    definition_spans: list[tuple[int, int]]
    names: list[tuple[int, str]]
    docstrings: list[tuple[int, str]]
    docstring_lines: frozenset[int]


def _parse(tree: ast.Module) -> _Parsed:
    functions: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    definition_spans: list[tuple[int, int]] = []
    names: list[tuple[int, str]] = []
    docstrings: list[tuple[int, str]] = []
    docstring_lines: set[int] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.append((node.lineno, node.id))
            continue
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue

        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            names.append((node.lineno, node.name))
            definition_spans.append((node.lineno, node.end_lineno or node.lineno))
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            functions.append(node)
            arguments = node.args
            names.extend(
                (argument.lineno, argument.arg)
                for argument in [
                    *arguments.posonlyargs,
                    *arguments.args,
                    *arguments.kwonlyargs,
                    arguments.vararg,
                    arguments.kwarg,
                ]
                if argument is not None
            )

        first = node.body[0] if node.body else None
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
            docstrings.append((first.lineno, first.value.value))
            docstring_lines.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))

    return _Parsed(functions, definition_spans, names, docstrings, frozenset(docstring_lines))


def _comments(source: str) -> list[tokenize.TokenInfo]:
    """The comment tokens, or nothing when the file does not tokenize (ruff reports that already)."""
    try:
        return [t for t in tokenize.generate_tokens(io.StringIO(source).readline) if t.type == tokenize.COMMENT]
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return []


def _comment_heavy_functions(parsed: _Parsed, source_lines: list[str], standalone: set[int]) -> list[Finding]:
    """Functions whose own body holds more standalone comment lines than code lines.

    A trailing comment shares its line with code and is therefore not a comment line here, while
    it does count as Dutch under measure 2: one line, two different questions. The body of a
    nested definition belongs to that definition and is left out, so one function is not counted
    twice over.

    The signature is not body code, so it does not count as code, while a comment anywhere below
    it does count: the comments this rule is about sit above the first statement, in a gap the
    first statement's line number does not reach.
    """
    findings: list[Finding] = []
    for node in parsed.functions:
        if not node.body or node.end_lineno is None:
            continue
        nested = {
            line
            for start, end in parsed.definition_spans
            if start > node.lineno and end <= node.end_lineno
            for line in range(start, end + 1)
        }
        own = set(range(node.lineno + 1, node.end_lineno + 1)) - parsed.docstring_lines - nested
        first_statement = node.body[0].lineno
        comments = len(own & standalone)
        code = sum(
            1 for line in own if line >= first_statement and line not in standalone and source_lines[line - 1].strip()
        )
        if comments > code:
            findings.append(
                Finding(
                    path="",
                    measure=COMMENT_HEAVY,
                    line_number=node.lineno,
                    detail=f"{node.name}: {comments} comment lines over {code} code lines",
                )
            )
    return findings


def scan_source(source: str, path: str) -> list[Finding]:
    """Every line of this file that counts, for all three measures."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    parsed = _parse(tree)

    findings: list[Finding] = []
    word = dutch_in_identifier(Path(path).stem)
    if word:
        findings.append(Finding(path, DUTCH_IDENTIFIERS, 1, f"module name: {word}"))
    for line_number, name in parsed.names:
        word = dutch_in_identifier(name)
        if word:
            findings.append(Finding(path, DUTCH_IDENTIFIERS, line_number, f"{name}: {word}"))

    comments = _comments(source)
    for token in comments:
        word = dutch_in_prose(token.string.lstrip("#"))
        if word:
            findings.append(Finding(path, DUTCH_COMMENTS, token.start[0], f"comment: {word}"))
    for line_number, text in parsed.docstrings:
        word = dutch_in_prose(text)
        if word:
            findings.append(Finding(path, DUTCH_COMMENTS, line_number, f"docstring: {word}"))

    standalone = {t.start[0] for t in comments if t.start[1] == len(t.line) - len(t.line.lstrip())}
    findings.extend(
        Finding(path, finding.measure, finding.line_number, finding.detail)
        for finding in _comment_heavy_functions(parsed, source.splitlines(), standalone)
    )
    return findings


def scan_file(path: Path, display: str | None = None) -> list[Finding]:
    return scan_source(path.read_text(encoding="utf-8"), display or path.as_posix())


def counts_per_file(findings: list[Finding]) -> dict[str, dict[str, int]]:
    """Per file, per measure, how many lines counted. A zero is left out."""
    tally: dict[str, dict[str, int]] = {}
    for finding in findings:
        per_measure = tally.setdefault(finding.path, {})
        per_measure[finding.measure] = per_measure.get(finding.measure, 0) + 1
    return tally


REPO = Path(__file__).resolve().parents[1]
PYTHON_DIR = REPO / "operations-manager" / "python"
BASELINE_FILE = REPO / "scripts" / "code-conventions-baseline.json"
#: Measured packages. opi/migrations is generated by alembic and is not written here.
ROOTS = ("opi", "tests")
SKIP_DIRECTORIES = frozenset({"migrations", "__pycache__"})

#: A file already on the baseline can hold hundreds of counting lines, and printing them all
#: pushes the count and the rule out of view. The count above the list says how many there are.
LINES_SHOWN = 25

HEADER = (
    "GENERATED by python3 scripts/check-code-conventions.py --update-baseline (task code-conventions-baseline). "
    "Per file the counts as they were. A count may drop and a file may disappear; raising one is a "
    "hand edit that has to be defended in the PR. See docs/development/coding-rules.md, chapter 11."
)


def python_files(python_dir: Path = PYTHON_DIR) -> list[Path]:
    found: list[Path] = []
    for root in ROOTS:
        found.extend(
            path for path in sorted((python_dir / root).rglob("*.py")) if SKIP_DIRECTORIES.isdisjoint(path.parts)
        )
    return found


def scan_tree(python_dir: Path = PYTHON_DIR) -> list[Finding]:
    findings: list[Finding] = []
    for path in python_files(python_dir):
        findings.extend(scan_file(path, path.relative_to(python_dir).as_posix()))
    return findings


def load_baseline(path: Path) -> dict[str, dict[str, int]]:
    """The recorded counts, or nothing when there is no baseline yet."""
    if not path.is_file():
        return {}
    document = json.loads(path.read_text(encoding="utf-8"))
    return document.get("files", {})


def write_baseline(path: Path, counts: dict[str, dict[str, int]]) -> None:
    files = {
        name: {measure: counts[name][measure] for measure in MEASURES if measure in counts[name]}
        for name in sorted(counts)
    }
    document = {"_": HEADER, "files": files}
    path.write_text(json.dumps(document, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def lowered(baseline: dict[str, dict[str, int]], current: dict[str, dict[str, int]]) -> dict[str, dict[str, int]]:
    """The baseline with every count that improved brought down, and nothing raised."""
    updated: dict[str, dict[str, int]] = {}
    for name, recorded in baseline.items():
        if name not in current:
            continue
        kept = {measure: min(count, current[name].get(measure, 0)) for measure, count in recorded.items()}
        kept = {measure: count for measure, count in kept.items() if count}
        if kept:
            updated[name] = kept
    return updated


def over_baseline(
    baseline: dict[str, dict[str, int]], current: dict[str, dict[str, int]]
) -> list[tuple[str, str, int, int]]:
    """Every count above what the baseline allows, as (file, measure, now, allowed)."""
    exceeded: list[tuple[str, str, int, int]] = []
    for name in sorted(current):
        for measure in MEASURES:
            now = current[name].get(measure, 0)
            allowed = baseline.get(name, {}).get(measure, 0)
            if now > allowed:
                exceeded.append((name, measure, now, allowed))
    return exceeded


def report(findings: list[Finding], baseline: dict[str, dict[str, int]], scanned: int) -> int:
    current = counts_per_file(findings)
    exceeded = over_baseline(baseline, current)
    if not exceeded:
        total = sum(sum(per_measure.values()) for per_measure in current.values())
        print(f"CLEAN {scanned} files, {total} counting lines, none above the baseline")
        return 0

    print(f"FAIL {len(exceeded)} counts above the baseline in {scanned} files:")
    for name, measure, now, allowed in exceeded:
        print(f"\n  {name}: {measure} is {now}, the baseline allows {allowed}")
        lines = [f for f in findings if f.path == name and f.measure == measure]
        for finding in lines[:LINES_SHOWN]:
            print(f"    line {finding.line_number}: {finding.detail}")
        if len(lines) > LINES_SHOWN:
            print(f"    ... and {len(lines) - LINES_SHOWN} more")
    print("\nThe rules are in docs/development/coding-rules.md. Existing code is not yours to clean up")
    print("here: fix what you added, or make the case in the PR and edit the baseline by hand.")
    return 1
