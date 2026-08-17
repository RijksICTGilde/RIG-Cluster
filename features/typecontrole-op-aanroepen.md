# Typecontrole op aanroepen

Pyright controleert in dit project sinds RC-40 of een aanroep bij zijn functie past.
Daarvoor stonden de twee regels die dat doen uit, waardoor getypeerde signatures wel
documentatie en editorhulp gaven maar niets afdwongen.

## Wat er nu wordt afgedwongen

In `operations-manager/python/pyproject.toml`:

```toml
reportCallIssue = true     # klopt deze aanroep met deze functie
reportArgumentType = true  # past dit argument bij dit parameter-type
```

Concreet vangt dit:

- een aanroep met een argument te veel of te weinig;
- een sleutelwoord dat de functie niet heeft (`Field(..., example=...)` was er 64x);
- `X | None` waar een `str` wordt gevraagd, zonder dat het pad ertussen None uitsluit.

Dat is geen netheid: de tien aanroepen die als eerste boven kwamen gaven allemaal een
`TypeError`, en op twee plekken werd die opgeslokt door een `except Exception` zodat de
operatie zichzelf "skipped" noemde. Zie `plans/typecontrole-op-aanroepen-aanzetten.md`
voor de volledige lijst en de afweging per stuk.

## Draaien

```bash
cd operations-manager/python
uv run pyright          # hoort 0 fouten te geven
```

Dit hoort bij de post-development validatie die al gold (`ruff check . --fix`,
`ruff format .`, `pyright`).

## Wat te doen als pyright je aanroep afwijst

In volgorde van voorkeur:

1. **Is het een echte fout?** Dan is het antwoord de aanroep, niet het type. Verreweg de
   meeste meldingen in RC-40 waren dit of het volgende punt.
2. **Garandeert de code al dat het niet None is, maar kan de checker dat niet zien?**
   Zet de controle dan zó neer dat hij het wél ziet: `if not x or not y:` in plaats van
   `if not all([x, y])`, of leg het gecontroleerde resultaat in een aparte variabele in
   plaats van het te hergebruiken over honderd regels heen.
3. **Beschrijft het type niet wat de code doet?** Verbreed het type. Voorbeeld:
   `record_clone(generation: int)` terwijl de schrijver eronder al jaren
   "never write None as generation" doet -- daar was `int | None` altijd de waarheid.
4. **Snapt pyright een afhankelijkheid niet?** Kijk eerst of een preciezere import het
   oplost (de prometheus-client deelt zijn klassen uit via een `__getattr__`-shim; de
   submodule importeren loste vier meldingen op). Pas daarna een gerichte
   `# pyright: ignore[<regel>]` mét de reden erbij.

Wat niet: een `# type: ignore` waar het rood is. RC-40 heeft er nul toegevoegd. "Pyright
snapt dit pakket niet" is een geldige reden, "geen tijd" niet.

## Wat nog uit staat

Zeventien andere pyright-regels staan nog op `false`. Ze zijn in RC-40 gemeten en
opgeschreven in `plans/typecontrole-op-aanroepen-aanzetten.md` (sectie "De overige
zeventien regels"), zodat zichtbaar is wat elke regel kost. Vijf ervan geven vandaag nul
fouten en kunnen dus zonder werk aan.

## Tests

- `tests/test_typed_call_sites.py` -- de aanroepen die niet bij hun functie pasten, plus
  twee AST-poorten die het patroon niet laten terugkomen.
- `tests/test_typed_argument_sites.py` -- de argumenten die niet bij hun parameter
  pasten.
