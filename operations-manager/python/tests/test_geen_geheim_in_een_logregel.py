"""Guard: een logregel toont geen stuk van een sleutel, token of wachtwoord.

Afgekapt sleutelmateriaal is nog steeds sleutelmateriaal, en met ``LOG_LEVEL=INFO`` in
productie gaat de hele logstroom naar Loki. Vier regels deden dit (de AGE-privesleutel,
API_TOKEN, OIDC_CLIENT_SECRET op twee plekken); ze noemen nu dat de waarde aanwezig is.
Twee regels in postgres.py logden ``sql[:100]``, waarin het wachtwoord van een CREATE ROLE
staat; die loggen nu de samenvatting uit ``_summarize_sql_statement``.

Dit loopt over de AST en niet over een regex op de brontekst, omdat een logregel over
meerdere regels mag lopen en de slice in een f-string zit.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

OPI_ROOT = Path(__file__).resolve().parent.parent / "opi"

LOG_METHODS = frozenset({"debug", "info", "warning", "warn", "error", "exception", "critical", "log"})
# sql staat erbij omdat een afgekapt statement het wachtwoord uit CREATE ROLE kan bevatten;
# een SQL-regel hoort als statement-soort plus doel in de log, niet als tekst.
GEVOELIGE_NAAM = re.compile(r"key|secret|token|password|sql|statement", re.IGNORECASE)


def _gesliced_naam(node: ast.AST) -> str | None:
    """De naam waar een slice op staat (``X_SECRET[:4]`` -> ``X_SECRET``), of None."""
    if not isinstance(node, ast.Subscript) or not isinstance(node.slice, ast.Slice):
        return None
    target = node.value
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    if isinstance(target, ast.Call) and isinstance(target.func, ast.Attribute):
        return target.func.attr
    return None


def _overtredingen_in(path: Path) -> list[str]:
    problemen: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in LOG_METHODS:
            continue
        for argument in [*node.args, *(keyword.value for keyword in node.keywords)]:
            for sub in ast.walk(argument):
                naam = _gesliced_naam(sub)
                if naam and GEVOELIGE_NAAM.search(naam):
                    problemen.append(f"{path}:{sub.lineno} logt een deel van {naam}")
    return problemen


def test_geen_logregel_toont_een_deel_van_een_geheim() -> None:
    overtredingen = [melding for path in sorted(OPI_ROOT.rglob("*.py")) for melding in _overtredingen_in(path)]
    assert overtredingen == [], "Log dat de waarde aanwezig is, niet een stuk ervan:\n" + "\n".join(overtredingen)


def test_de_guard_vindt_een_overtreding(tmp_path: Path) -> None:
    """Zonder deze toets zou een guard die niets meer ziet ook groen staan."""
    bron = tmp_path / "leaky.py"
    bron.write_text('logger.info(f"token: {settings.API_TOKEN[:5]}...")\n', encoding="utf-8")
    assert len(_overtredingen_in(bron)) == 1
