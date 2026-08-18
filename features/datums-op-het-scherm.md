# Datums op het scherm

**Status**: Implemented
**Date**: 2026-08-18
**Related**: [Eén voortgangsweergave](./task-progress-view.md)

## Wat het is

Elk tijdstip dat het portaal toont, gaat door één filter: `dutch_date`
(`format_dutch_date` in `opi/core/template_helpers.py`, geregistreerd in
`opi/core/templates_lotc.py`). Dat filter doet twee dingen die geen enkel sjabloon zelf
hoort te doen:

1. **Omrekenen naar onze tijd.** Alles wat we opslaan staat in UTC; het filter zet het om
   naar Europe/Amsterdam.
2. **In het Nederlands schrijven.** `17 september 2026 23:18`, met de maand voluit.

## Gebruik

```jinja
{{ taak.gestart | dutch_date }}                      {# 17 september 2026 23:18 #}
{{ aanvraag.date | dutch_date(include_time=False) }} {# 17 september 2026 #}
{{ taak.gestart | dutch_date(short_month=True) }}    {# 17 sep 2026 23:18 #}
```

Een lege waarde levert `-` op. Je hoeft er dus niet zelf een `{% if %}` omheen te zetten.

| Optie | Wanneer |
|---|---|
| standaard | overal waar de datum in gewone tekst staat |
| `include_time=False` | als alleen de dag telt (een aanvraag, een aanmaakdatum) |
| `short_month=True` | in een DICHTE, herhaalde context: een tabel met veel kolommen |

`short_month` is er om de breedte, niet om de smaak: in de takentabel is
`18 september 2026 01:40` 174px breed en `18 sep 2026 01:40` 126px, en die tabel heeft zes
kolommen te verdelen over 705px (bij een venster van 1280). Het is een optie op hetzelfde
filter en geen tweede formatteerfunctie, zodat de omrekening en de maandnamen op één plek
blijven staan.

## Waar de tijdstippen vandaan komen

De takendienst en de runsdienst schrijven hun tijdstippen met de servertijd van de
database, in een `timestamptz`-kolom. De database staat op `Etc/UTC`, dus wat de API en de
sjablonen binnenkrijgen is bijvoorbeeld `2026-09-17T21:18:55.951682+00:00`. Zonder
omrekening zie je in de zomer twee uur te vroeg en in de winter één uur; de LOG van
diezelfde taak schrijft wel in onze tijd (de pod staat op Europe/Amsterdam), dus zonder
het filter spreken de tabel en de log elkaar tegen.

## Zelf afkappen mag niet

Dit ging drie keer eerder mis met dezelfde truc:

```jinja
{{ (item.gestart or "")[:16] | replace("T", " ") }}   {# FOUT #}
```

Dat toont het UTC-getal zonder dat erbij te zeggen -- de `+00:00` valt net buiten die
zestien tekens -- en het loopt langs de enige plek die van tijdzones weet.

`tests/test_dates_go_through_one_filter.py` bewaakt dat. Die test verzamelt ELKE `[:N]` in
elk sjabloon en legt ze langs een lijst van afkappingen die geen tijdstip zijn (initialen,
de eerste drie teamleden, een te lange foutmelding). Staat er iets nieuws in een sjabloon,
dan valt de test en is de vraag: is dit een tijdstip? Dan via `dutch_date`. Zo niet, dan
hoort het met een reden op die lijst.

## Afhankelijkheden

Geen. `zoneinfo` zit in de standaardbibliotheek.
