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

Wat wel mag: een smalle, eigen uitzondering die zijn boodschap meegeeft aan alles wat
GEEN 5xx is - een 4xx, of de 200 waarmee een formulier of een modal terugkomt.
``SkopeoValidationError("tag mag geen spaties bevatten")`` IS de tekst voor de lezer, en
het plan vraagt uitdrukkelijk om een eigen boodschap waar die hoort.

De volledige fout is niet weg - hij staat in de log, met hetzelfde kenmerk dat de
gebruiker te zien krijgt. Zie :mod:`opi.core.errors`.

Welke deuren de sweep kent
--------------------------

Niet "elke deur naar buiten": deze test kent er vijf, en dat is wat hij dekt.

============================  =======================================================
deur                          waarom hij erin zit
============================  =======================================================
``HTTPException``             de vorm uit de storing zelf
antwoordklassen               ``JSONResponse``/``HTMLResponse``/``PlainTextResponse``/
                              ``Response`` - twee gereedschapsroutes gaven hun
                              uitzondering mee in de body
sjabloon-render               ``render(...)``/``TemplateResponse(...)`` - de weg
                              waarlangs een browser vrijwel elke pagina krijgt
een teruggegeven waarde       een hulpfunctie die een ``dict`` oplevert die verderop
                              in een antwoord belandt
``list.append(...)``          een per-onderdeel regel in een verzamelde body
============================  =======================================================

Wat de sweep NIET ziet, en dus niet belooft: een waarde die via een attribuut of een
buitenstaand object weglekt, iets dat eerst ``opi/web``/``opi/api`` verlaat (de
managerlaag valt buiten de gemeten mappen), en een uitzondering die als gegeven wordt
opgeslagen en pas bij een later verzoek wordt getoond.

De besmetting reist mee de handler UIT. ``ctx["usage_error"] = str(e)`` stond in het
except-blok en de ``render`` die ``ctx`` uittekende stond eronder, buiten de ``try``:
een analyse die bij de handler stopt ziet dat niet. De naam van de uitzondering zelf
(``e``) blijft wel binnen de handler - Python maakt hem aan het eind van het blok los -
dus die kan er per definitie niet uit ontsnappen.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

#: De mappen die een antwoord aan een aanroeper opstellen.
GEMETEN_MAPPEN = ("opi/web", "opi/api")

#: Vangsten waarvan de inhoud onbekend is.
BREDE_VANGSTEN = frozenset({"Exception", "BaseException"})

#: Antwoordklassen: wat hier in de body gaat, gaat naar de aanroeper.
ANTWOORDKLASSEN = frozenset({"JSONResponse", "HTMLResponse", "PlainTextResponse", "Response"})

#: De sjabloon-deur. Alles wat in de context van een render staat kan op het scherm komen,
#: en dat is de weg waarlangs een browser vrijwel elke pagina krijgt.
SJABLOONDEUREN = frozenset({"render", "TemplateResponse"})

#: Aanroepen die een tekst naar buiten dragen.
UITGANGEN = frozenset({"HTTPException"}) | ANTWOORDKLASSEN | SJABLOONDEUREN

#: De status van iets dat er zelf geen meekrijgt: een antwoordklasse zonder
#: ``status_code``, een teruggegeven waarde, een regel in een lijst. ``HTTPException``
#: heeft er altijd een (het eerste argument), dus dit raakt hem niet.
STANDAARDSTATUS = 200

#: Het teken waarmee een plek zich aan de sweep onttrekt. Alleen voor een plek die
#: gewogen is, met de reden erachter in dezelfde regel - een uitzondering die je moet
#: opschrijven blijft zichtbaar, een gat in het model niet.
GEWOGEN_TEKEN = "# foutmelding-gewogen:"


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


def _besmet(begin: set[str], bereik: ast.AST) -> set[str]:
    """Elke naam in ``bereik`` die de uitzondering draagt, ook via een tussenstap.

    ``error_msg = str(e)`` en daarna ``detail=f"...{error_msg}"`` was de vorm waarin het
    negen keer in de webrouter stond; een controle op alleen de gebonden naam ziet die
    niet. Vandaar een vast punt: alles wat uit iets besmets wordt toegekend is besmet.
    Ook een schrijf IN een object telt (``ctx["usage_error"] = str(e)`` besmet ``ctx``) -
    dat was de weg waarlangs het resourcegebruik zijn Prometheus-fout op de pagina zette.
    """
    besmet = set(begin)
    gegroeid = True
    while gegroeid:
        gegroeid = False
        for knoop in ast.walk(bereik):
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


def _dracht(aanroep: ast.Call) -> list[ast.expr]:
    """Wat deze aanroep aan de aanroeper meegeeft."""
    naam = _naam_van(aanroep.func)
    if naam == "HTTPException":
        for kw in aanroep.keywords:
            if kw.arg == "detail":
                return [kw.value]
        return [aanroep.args[1]] if len(aanroep.args) > 1 else []
    if naam in ANTWOORDKLASSEN:
        for kw in aanroep.keywords:
            if kw.arg == "content":
                return [kw.value]
        return [aanroep.args[0]] if aanroep.args else []
    # Een render: alles wat erin gaat kan eruit komen. Welke sleutel het sjabloon
    # afdrukt staat in het sjabloon, niet hier.
    return list(aanroep.args) + [kw.value for kw in aanroep.keywords if kw.arg != "status_code"]


def _bevat_uitgang(knoop: ast.AST) -> bool:
    return any(_uitgang(n) for n in ast.walk(knoop))


#: Wat een eigen naamruimte heeft, en dus de reikwijdte van een besmette naam begrenst.
BEREIKEN = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)


def _ouders(boom: ast.AST) -> dict[int, ast.AST]:
    ouder: dict[int, ast.AST] = {}
    for knoop in ast.walk(boom):
        for kind in ast.iter_child_nodes(knoop):
            ouder[id(kind)] = knoop
    return ouder


def _bereik_van(handler: ast.ExceptHandler, ouder: dict[int, ast.AST], boom: ast.AST) -> ast.AST:
    """De functie waar deze handler in staat: zover reist een besmette naam."""
    knoop = ouder.get(id(handler))
    while knoop is not None and not isinstance(knoop, BEREIKEN):
        knoop = ouder.get(id(knoop))
    return knoop if knoop is not None else boom


def _deuren(knoop: ast.AST) -> list[tuple[int, list[ast.expr], int | None, str]]:
    """De uitgangen in deze knoop: (regel, wat er doorheen gaat, status, welke deur)."""
    aanroep = _uitgang(knoop)
    if aanroep is not None:
        return [(aanroep.lineno, _dracht(aanroep), _status(aanroep), str(_naam_van(aanroep.func)))]
    if isinstance(knoop, ast.Return) and knoop.value is not None and not _bevat_uitgang(knoop.value):
        # Een hulpfunctie die een dict oplevert; de aanroeper zet hem in een antwoord.
        # Staat er een antwoordklasse in, dan meet die deur hem al - met haar eigen status.
        return [(knoop.lineno, [knoop.value], STANDAARDSTATUS, "return")]
    if isinstance(knoop, ast.Call) and _naam_van(knoop.func) == "append":
        return [(knoop.lineno, list(knoop.args), STANDAARDSTATUS, "append")]
    return []


def _is_gewogen(handler: ast.ExceptHandler, regels: list[str]) -> bool:
    """Of dit blok een gewogen uitzondering op de regel draagt, met reden."""
    eind = handler.end_lineno or handler.lineno
    for regel in regels[handler.lineno - 1 : eind]:
        if GEWOGEN_TEKEN in regel:
            return bool(regel.split(GEWOGEN_TEKEN, 1)[1].strip())
    return False


def overtredingen(bron: str, herkomst: str) -> list[str]:
    """Elke plek in ``bron`` waar de tekst van een uitzondering naar buiten reist."""
    boom = ast.parse(bron)
    ouder = _ouders(boom)
    regels = bron.splitlines()
    gevonden: set[str] = set()
    for handler in ast.walk(boom):
        if not isinstance(handler, ast.ExceptHandler) or handler.name is None:
            continue
        if _is_gewogen(handler, regels):
            continue
        breed = _is_breed(handler)
        bereik = _bereik_van(handler, ouder, boom)
        # Binnen het blok draagt de uitzondering zelf mee; buiten het blok alleen wat
        # er in een naam is achtergebleven - Python maakt ``e`` na de handler los.
        binnen = _besmet({handler.name}, handler)
        ontsnapt = binnen - {handler.name}
        buiten = _besmet(ontsnapt, bereik) if ontsnapt else set()
        in_handler = {id(n) for n in ast.walk(handler)}
        for knoop in ast.walk(bereik):
            besmet = binnen if id(knoop) in in_handler else buiten
            if not besmet:
                continue
            for lineno, dracht, status, deur in _deuren(knoop):
                if not any(_namen(d) & besmet for d in dracht):
                    continue
                # Een smalle vangst mag zijn eigen boodschap meegeven zolang het geen 5xx
                # is. Een status die niet uit de aanroep is af te lezen telt als onbekend,
                # en dus als fout: een grendel die bij twijfel doorlaat is geen grendel.
                if not breed and status is not None and status < 500:
                    continue
                waarom = "brede vangst" if breed else f"status {status}"
                tekst = "; ".join(ast.unparse(d) for d in dracht)[:100]
                gevonden.add(f"{herkomst}:{lineno} [{deur}] ({waarom}): {tekst}")
    return sorted(gevonden)


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

    def test_de_gewogen_uitzonderingen_zijn_op_een_hand_te_tellen(self) -> None:
        """Elke ``# foutmelding-gewogen:`` is een plek die de regel niet volgt.

        Ze mogen bestaan - een health-check die per peer meldt waarom hij niet antwoordde
        zet die tekst naast de url van diezelfde peer, die de beheerder zelf invoerde -
        maar ze horen zeldzaam en opzoekbaar te blijven. Wordt dit getal groter, dan is
        de vraag of de regel of de code moet wijken, en niet of er nog een uitzondering bij kan.
        """
        wortel = _wortel()
        gewogen = [
            f"{pad.relative_to(wortel)}:{nr}"
            for pad in _gemeten_bestanden()
            for nr, regel in enumerate(pad.read_text().splitlines(), start=1)
            if GEWOGEN_TEKEN in regel
        ]
        assert len(gewogen) <= 2, "gewogen uitzonderingen:\n" + "\n".join(gewogen)


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


class TestDeDrieDeurenErbij:
    """Per deur een tegenproef, met de vorm die er vandaag achter stond."""

    #: De vorm uit ``project_resource_usage_fragment``: de toekenning staat in het
    #: except-blok, de render eronder - buiten de ``try``.
    SJABLOON = (
        "def fragment(request, naam):\n"
        "    ctx = {}\n"
        "    try:\n"
        "        ctx['usage'] = meet()\n"
        "    except Exception as e:\n"
        "        ctx['usage_error'] = str(e)\n"
        "    return render(request, template='bg/_resource-usage.html.j2', context=ctx)\n"
    )

    #: De vorm uit ``_restore_database_with_versioning``: een dict die verderop in een
    #: 500-body belandt.
    TERUGGEGEVEN_DICT = (
        "def zet_terug():\n"
        "    try:\n"
        "        doe()\n"
        "    except Exception as e:\n"
        "        return {'success': False, 'error': f'Database restore error: {e}'}\n"
    )

    #: De vorm uit ``get_project_logs``: een regel per onderdeel in een verzamelde body.
    LIJSTREGEL = (
        "def logs():\n"
        "    results = []\n"
        "    for depl in deployments:\n"
        "        try:\n"
        "            haal_op()\n"
        "        except Exception as e:\n"
        "            results.append({'lines': [], 'error': str(e)})\n"
        "    return JSONResponse(content={'results': results})\n"
    )

    def test_de_sjabloondeur_wordt_gezien(self) -> None:
        assert overtredingen(self.SJABLOON, "toets.py")

    def test_de_sjabloondeur_ziet_hem_ook_buiten_de_try(self) -> None:
        """De render staat NA het except-blok: een analyse die daar stopt ziet niets."""
        (gevonden,) = overtredingen(self.SJABLOON, "toets.py")
        assert "[render]" in gevonden
        assert gevonden.startswith("toets.py:7"), gevonden

    def test_een_smalle_vangst_mag_zijn_melding_wel_renderen(self) -> None:
        """De uitnodigingspagina toont zijn eigen InviteError op een 200; dat is de tekst."""
        bron = (
            "def pagina(request):\n"
            "    try:\n"
            "        controleer()\n"
            "    except InviteError as e:\n"
            "        return render(request, template='bg/invite-error.html.j2', context={'melding': str(e)})\n"
        )
        assert not overtredingen(bron, "toets.py")

    def test_de_teruggegeven_dict_wordt_gezien(self) -> None:
        (gevonden,) = overtredingen(self.TERUGGEGEVEN_DICT, "toets.py")
        assert "[return]" in gevonden

    def test_een_teruggegeven_antwoord_telt_bij_zijn_eigen_deur(self) -> None:
        """``return JSONResponse(..., status_code=400)`` is geen kale return: de
        antwoordklasse draagt de status, en een smalle vangst mag daar zijn melding kwijt."""
        bron = (
            "def route():\n"
            "    try:\n"
            "        doe()\n"
            "    except SkopeoValidationError as e:\n"
            "        return JSONResponse(content={'error': str(e)}, status_code=400)\n"
        )
        assert not overtredingen(bron, "toets.py")

    def test_de_lijstregel_wordt_gezien(self) -> None:
        (gevonden,) = overtredingen(self.LIJSTREGEL, "toets.py")
        assert "[append]" in gevonden

    def test_de_naam_van_de_uitzondering_ontsnapt_niet_uit_zijn_blok(self) -> None:
        """Python maakt ``e`` na de handler los, dus mag een tweede blok met dezelfde
        naam er niet door besmet raken - anders is elke 409 naast een brede vangst rood."""
        bron = (
            "def route():\n"
            "    try:\n"
            "        doe()\n"
            "    except RuntimeError as e:\n"
            "        raise HTTPException(status_code=409, detail=str(e)) from e\n"
            "    except Exception as e:\n"
            "        raise HTTPException(status_code=500, detail='Probeer het opnieuw.') from e\n"
        )
        assert not overtredingen(bron, "toets.py")


class TestDeGewogenUitzondering:
    """Een plek mag zich onttrekken, maar alleen met de reden erbij."""

    BRON = (
        "def health():\n"
        "    for peer in peers:\n"
        "        entry = {'url': peer.url}\n"
        "        try:\n"
        "            controleer(peer)\n"
        "        except Exception as exc:\n"
        "TEKEN"
        "            entry['status'] = str(exc)\n"
        "        results.append(entry)\n"
    )

    def test_zonder_teken_is_het_gewoon_een_overtreding(self) -> None:
        assert overtredingen(self.BRON.replace("TEKEN", ""), "toets.py")

    def test_met_teken_en_reden_is_de_plek_gewogen(self) -> None:
        teken = "            # foutmelding-gewogen: de fout van de peer naast de url van diezelfde peer\n"
        assert not overtredingen(self.BRON.replace("TEKEN", teken), "toets.py")

    def test_een_teken_zonder_reden_telt_niet(self) -> None:
        """Anders is het een uitzetknop in plaats van een gewogen uitzondering."""
        assert overtredingen(self.BRON.replace("TEKEN", "            # foutmelding-gewogen:\n"), "toets.py")


class TestWatWelMag:
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
