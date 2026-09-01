"""Een uitzondering mag niet meereizen naar de aanroeper.

De grendel op wat tijdens de databasestoring op het scherm stond. De melding was
``Template error: [Errno 111] Connect call failed ('172.30.19.11', 5432)``: een intern
IP-adres en de databasepoort, in een ``detail`` dat rechtstreeks uit de opgevangen
uitzondering kwam. Zonder deze test staat daar over een half jaar weer een IP-adres.

De regel die de test afdwingt, in twee helften:

* uit een BREDE vangst (``except Exception``) mag de tekst van de uitzondering nooit
  in een antwoord komen. Wat daar binnenvalt is per definitie onbekend, dus is elke
  aanname over de inhoud een gok;
* naar een **5xx** mag hij ook uit een smalle vangst niet mee. Een 5xx komt uit de
  infrastructuur, en dat is precies de laag waarvan de aanroeper niets hoort te weten.

Beide gelden voor elke deur naar buiten, niet alleen voor ``detail=``: twee
gereedschapsroutes gaven hun uitzondering mee in de body van een ``JSONResponse``.

Wat wel mag: een smalle, eigen uitzondering die zijn boodschap aan een **4xx** meegeeft.
``SkopeoValidationError("tag mag geen spaties bevatten")`` IS de tekst voor de lezer, en
het plan vraagt uitdrukkelijk om een eigen boodschap waar die hoort.

De volledige fout is niet weg - hij staat in de log, met hetzelfde kenmerk dat de
gebruiker te zien krijgt. Zie :mod:`opi.core.errors`.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

#: De mappen die een antwoord aan een aanroeper opstellen.
GEMETEN_MAPPEN = ("opi/web", "opi/api")

#: Vangsten waarvan de inhoud onbekend is.
BREDE_VANGSTEN = frozenset({"Exception", "BaseException"})

#: Aanroepen die een tekst naar buiten dragen. Niet alleen ``HTTPException``: twee
#: gereedschapsroutes gaven hun uitzondering mee in een ``JSONResponse``-body, wat
#: hetzelfde lek is door een andere deur.
UITGANGEN = frozenset({"HTTPException", "JSONResponse", "HTMLResponse", "PlainTextResponse", "Response"})

#: De status van een antwoordklasse die er zelf geen meekrijgt. ``HTTPException`` heeft er
#: altijd een (het eerste argument), dus dit raakt alleen de responses.
STANDAARDSTATUS = 200


def _wortel() -> Path:
    """De map met ``opi/`` erin, ongeacht waarvandaan pytest draait."""
    return Path(__file__).resolve().parent.parent


def _namen(knoop: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(knoop) if isinstance(n, ast.Name)}


def _is_breed(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    soorten = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any(isinstance(s, ast.Name) and s.id in BREDE_VANGSTEN for s in soorten)


def _besmet(handler: ast.ExceptHandler) -> set[str]:
    """Elke naam in dit blok die de uitzondering draagt, ook via een tussenstap.

    ``error_msg = str(e)`` en daarna ``detail=f"...{error_msg}"`` was de vorm waarin het
    negen keer in de webrouter stond; een controle op alleen de gebonden naam ziet die
    niet. Vandaar een vast punt: alles wat uit iets besmets wordt toegekend is besmet.
    """
    assert handler.name is not None
    besmet = {handler.name}
    gegroeid = True
    while gegroeid:
        gegroeid = False
        for knoop in ast.walk(handler):
            if isinstance(knoop, ast.Assign | ast.AnnAssign | ast.AugAssign):
                waarde = knoop.value
                if waarde is None or not (_namen(waarde) & besmet):
                    continue
                doelen = knoop.targets if isinstance(knoop, ast.Assign) else [knoop.target]
                for doel in doelen:
                    for naam in _namen(doel):
                        if naam not in besmet:
                            besmet.add(naam)
                            gegroeid = True
    return besmet


def _naam_van(functie: ast.expr) -> str | None:
    if isinstance(functie, ast.Name):
        return functie.id
    return functie.attr if isinstance(functie, ast.Attribute) else None


def _uitgang(knoop: ast.AST) -> ast.Call | None:
    if not isinstance(knoop, ast.Call):
        return None
    return knoop if _naam_van(knoop.func) in UITGANGEN else None


def _is_http_exception(aanroep: ast.Call) -> bool:
    return _naam_van(aanroep.func) == "HTTPException"


def _status(aanroep: ast.Call) -> int | None:
    """De statuscode, of None als hij niet uit de aanroep zelf is af te lezen."""
    voor_status: ast.expr | None = aanroep.args[0] if (_is_http_exception(aanroep) and aanroep.args) else None
    for kw in aanroep.keywords:
        if kw.arg == "status_code":
            voor_status = kw.value
    if voor_status is None:
        return None if _is_http_exception(aanroep) else STANDAARDSTATUS
    if isinstance(voor_status, ast.Constant) and isinstance(voor_status.value, int):
        return voor_status.value
    return None


def _tekst(aanroep: ast.Call) -> ast.expr | None:
    """Wat deze aanroep aan de aanroeper meegeeft: ``detail`` of de body."""
    sleutel = "detail" if _is_http_exception(aanroep) else "content"
    for kw in aanroep.keywords:
        if kw.arg == sleutel:
            return kw.value
    if _is_http_exception(aanroep):
        return aanroep.args[1] if len(aanroep.args) > 1 else None
    return aanroep.args[0] if aanroep.args else None


def overtredingen(bron: str, herkomst: str) -> list[str]:
    """Elke plek in ``bron`` waar de tekst van een uitzondering naar buiten reist."""
    gevonden: list[str] = []
    for handler in ast.walk(ast.parse(bron)):
        if not isinstance(handler, ast.ExceptHandler) or handler.name is None:
            continue
        breed = _is_breed(handler)
        besmet = _besmet(handler)
        for knoop in ast.walk(handler):
            aanroep = _uitgang(knoop)
            if aanroep is None:
                continue
            tekst = _tekst(aanroep)
            if tekst is None or not (_namen(tekst) & besmet):
                continue
            status = _status(aanroep)
            # Een smalle vangst mag zijn eigen boodschap meegeven zolang het geen 5xx is.
            # Een status die niet uit de aanroep is af te lezen telt als onbekend, en dus
            # als fout: een grendel die bij twijfel doorlaat is geen grendel.
            if not breed and status is not None and status < 500:
                continue
            waarom = "brede vangst" if breed else f"status {status}"
            gevonden.append(f"{herkomst}:{aanroep.lineno} ({waarom}): {ast.unparse(tekst)[:100]}")
    return gevonden


def _gemeten_bestanden() -> list[Path]:
    wortel = _wortel()
    return sorted(pad for map_ in GEMETEN_MAPPEN for pad in (wortel / map_).rglob("*.py"))


class TestDeGrendel:
    def test_geen_enkele_uitzondering_reist_mee_naar_buiten(self) -> None:
        wortel = _wortel()
        gevonden: list[str] = []
        for pad in _gemeten_bestanden():
            gevonden += overtredingen(pad.read_text(), str(pad.relative_to(wortel)))
        assert not gevonden, "de uitzondering reist mee naar de aanroeper:\n" + "\n".join(gevonden)

    def test_er_is_iets_te_meten(self) -> None:
        """Een sweep over nul bestanden slaagt vacuum; dit is de bodem eronder."""
        assert len(_gemeten_bestanden()) > 20


class TestDeGrendelWerkt:
    """De tegenproef: zet de oude vorm terug en de grendel moet hem zien."""

    def test_de_vorm_uit_de_storing_wordt_gezien(self) -> None:
        bron = (
            "try:\n"
            "    render()\n"
            "except Exception as e:\n"
            '    raise HTTPException(status_code=500, detail=f"Template error: {e!s}")\n'
        )
        assert overtredingen(bron, "toets.py")

    def test_een_tussenstap_verbergt_hem_niet(self) -> None:
        bron = (
            "try:\n"
            "    render()\n"
            "except Exception as e:\n"
            "    error_msg = str(e)\n"
            "    if hasattr(e, 'lineno'):\n"
            "        error_msg = f'Line {e.lineno}: {error_msg}'\n"
            '    raise HTTPException(status_code=500, detail=f"Template error: {error_msg}")\n'
        )
        assert overtredingen(bron, "toets.py")

    def test_str_van_de_uitzondering_naar_een_5xx_wordt_gezien(self) -> None:
        bron = "try:\n    doe()\nexcept RuntimeError as e:\n    raise HTTPException(status_code=500, detail=str(e))\n"
        assert overtredingen(bron, "toets.py")

    def test_een_onbekende_status_telt_als_fout(self) -> None:
        bron = "try:\n    doe()\nexcept ValueError as e:\n    raise HTTPException(status_code=code, detail=str(e))\n"
        assert overtredingen(bron, "toets.py")

    def test_een_uitzondering_in_een_json_body_wordt_gezien(self) -> None:
        """De andere deur: twee gereedschapsroutes gaven hem mee in een JSONResponse."""
        bron = (
            "try:\n"
            "    versleutel()\n"
            "except Exception as e:\n"
            '    return JSONResponse(content={"error": f"Encryption failed: {e!s}"}, status_code=500)\n'
        )
        assert overtredingen(bron, "toets.py")

    def test_een_body_zonder_status_telt_als_200(self) -> None:
        """Een Response krijgt 200 als er niets bij staat; uit een BREDE vangst mag ook dat niet."""
        breed = 'try:\n    doe()\nexcept Exception as e:\n    return HTMLResponse(content=f"<p>{e}</p>")\n'
        smal = 'try:\n    doe()\nexcept ProjectSchemaError as e:\n    return HTMLResponse(content=f"<p>{e}</p>")\n'
        assert overtredingen(breed, "toets.py")
        assert not overtredingen(smal, "toets.py"), "een validatiemelding op een 200 is de tekst voor de lezer"

    @pytest.mark.parametrize(
        "bron",
        [
            # Een smalle, eigen uitzondering mag zijn boodschap aan een 4xx meegeven.
            "try:\n    doe()\nexcept SkopeoValidationError as e:\n    raise HTTPException(status_code=400, detail=str(e))\n",
            # Een boodschap die de aanroeper zelf aanleverde is geen uitzondering.
            'try:\n    doe()\nexcept Exception as e:\n    raise HTTPException(status_code=500, detail=f"Project {naam} kon niet worden opgehaald.")\n',
            # De uitzondering in de LOG is juist de bedoeling.
            'try:\n    doe()\nexcept Exception as e:\n    logger.exception("mislukt: %s", e)\n    raise HTTPException(status_code=500, detail="Probeer het opnieuw.")\n',
        ],
    )
    def test_wat_wel_mag_blijft_stil(self, bron: str) -> None:
        assert not overtredingen(bron, "toets.py")
