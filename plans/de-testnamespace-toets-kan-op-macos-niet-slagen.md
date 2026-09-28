# De namespace-toets van de testdatabaseveeg kan op macOS niet slagen

**Status**: plan, nog niets gebouwd.
**Datum**: 2026-09-16
**Aanleiding**: `tests/test_orm_database_opruiming.py::test_de_namespace_is_die_van_dit_proces` is rood op main sinds RC-195 (PR #173, vandaag gemerged). Gemeten op main tijdens het mergen van RC-167.

## Wat er gebeurt

De veeg van verweesde testdatabases herkent de maker van een database aan zijn pid, en een pid zegt alleen iets binnen zijn eigen PID-namespace. `conftest._pid_namespace()` leest daarom het inode-nummer van `/proc/self/ns/pid` en zet dat in de databasenaam.

Die functie is zelf in orde. Ze vangt het geval af dat het pad niet bestaat:

```python
def _pid_namespace() -> str:
    """Alleen binnen deze namespace zegt ``os.kill`` iets over een pid."""
    try:
        return str(os.stat("/proc/self/ns/pid").st_ino)
    except OSError:
        return "0"
```

De test doet diezelfde `os.stat` nog een keer, maar dan zonder die vangst:

```python
def test_de_namespace_is_die_van_dit_proces() -> None:
    assert _pid_namespace() == str(os.stat("/proc/self/ns/pid").st_ino)
```

`/proc` bestaat op macOS niet, dus de test valt om met `FileNotFoundError` voor hij aan zijn assertie toekomt. Niet de code faalt, de meting faalt. Op Linux, en dus in CI en in de containers, is hij groen; alleen wie de suite op zijn eigen Mac draait ziet hem rood.

## Waarom `"0"` op macOS het goede antwoord is, en dus getoetst hoort te worden

De terugval is geen noodgreep maar een uitspraak: er is hier één vlakke namespace, dus `os.kill` is gezaghebbend over elke pid die we tegenkomen. Dat klopt op macOS. De consequentie staat in `_is_wees`: bij een gelijke namespace wordt `_maker_leeft(pid)` de doorslaggevende toets, en bij een andere namespace valt hij terug op de leeftijd van de database. Op een machine zonder `/proc` dragen alle namen `0`, komt elke run in de eerste tak terecht, en beoordeelt `os.kill` of de maker nog leeft. Dat is precies wat je daar wilt.

Dat gedrag is vandaag nergens vastgelegd. De enige test erover meet de Linux-tak, en doet dat op een manier die elders niet eens kan draaien.

## Wat we bouwen

De test gaat het contract van `_pid_namespace()` uitdrukken op allebei de paden, in plaats van één platform te veronderstellen.

1. Waar `/proc/self/ns/pid` bestaat: de uitkomst is dat inode-nummer, als tekst. Dat is de bestaande assertie.
2. Waar het niet bestaat: de uitkomst is `"0"`. Dat is nieuw en dekt de tak die de `except OSError` neerzet.

Beide takken horen op elke machine te draaien, niet alleen de tak die bij het platform hoort. De tweede is af te dwingen zonder het platform na te bootsen, want `_pid_namespace` leest via `os.stat` en die is te laten struikelen. Kies zelf of dat met een monkeypatch op `os.stat` gaat of door het pad uit de functie te tillen naar een constante; als het tweede, zeg dan in de PR waarom de vorm van de functie moest wijzigen voor een test.

Een `skipif` op het platform is nadrukkelijk niet de bedoeling. Dan blijft de helft van het contract op elke machine ongemeten, en het is juist de terugvaltak die niemand ooit ziet falen.

## Wat we niet doen

`_pid_namespace()` zelf blijft zoals hij is. De functie doet het goede; alleen de meting deugt niet. Ook `_is_wees`, `_maker_leeft` en de veeg blijven ongemoeid: dit is geen herziening van de regeling, alleen van de test die haar zou moeten vastleggen.

## Klaar als

- `uv run pytest tests/test_orm_database_opruiming.py` is groen op macOS en op Linux.
- Beide takken van `_pid_namespace()` worden op beide platforms gemeten, dus er staat geen `skipif` op het platform in.
- De hele unitsuite, `ruff check`, `ruff format` en `pyright` zijn schoon.
