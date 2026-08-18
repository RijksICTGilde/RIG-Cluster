# De takentabel knijpt de datum, en het is onduidelijk in welke tijdzone hij staat

Op `/projects/<naam>/taken` breken de kolommen Gestart en Beeindigd hun datum af over meerdere regels. Dat oogt slordig, en er zit een tweede vraag onder: klopt de getoonde tijd wel met onze eigen tijdzone?

## Wat er gemeten is

**De kolomverdeling geeft de datum de smalste plek.** `opi/templates_lotc/bg/_tasks.html.j2:19`:

```
<c-table columns="2fr 2fr 1fr 2fr 1fr 1fr">
             Soort  Deployment Status Door Gestart Beeindigd
```

Gestart en Beeindigd krijgen elk `1fr`, het smalst van de zes, terwijl de inhoud `2026-08-18 09:20` is: zestien tekens, de langste vaste waarde in de tabel. Status is een kort label en Soort een woord of twee, maar die krijgen meer.

**De tijd wordt niet geformatteerd maar afgekapt.** Regel 74 en 75:

```jinja
{{ (item.gestart or "")[:16] | replace("T", " ") }}
```

Dat neemt de eerste zestien tekens van de ruwe tijdstempel en vervangt de T door een spatie. Er wordt dus niets omgerekend, en een eventuele tijdzone-aanduiding valt buiten die zestien tekens weg.

**Er bestaat al een filter dat dit hoort te doen.** `format_dutch_date` in `opi/core/template_helpers.py:41`, geregistreerd als `dutch_date` in `opi/core/templates_lotc.py:136`, en gebruikt op de goedkeuringspagina (`last.date | dutch_date(include_time=False)`). De takentabel gebruikt het niet.

**De waarde komt rechtstreeks uit de taak**: `router_tasks.py:108` zet `"gestart": task.get("created_at")`. Zoek uit in welke zone die wordt opgeslagen. Gemeten in de productiepod is de lokale tijd Europe/Amsterdam en loopt UTC twee uur achter; staat `created_at` in UTC en wordt hij onbewerkt getoond, dan ziet een gebruiker consequent twee uur te vroeg. **Meet dat, gok het niet**: vergelijk een taak in de tabel met het tijdstip in de log van diezelfde taak.

## Wat er moet gebeuren

1. **Stel de tijdzone vast.** Waarin slaat de takendienst zijn tijdstempels op, wat toont de tabel, en klopt dat met elkaar? Als er omgerekend moet worden: doe dat op één plek, niet per sjabloon.
2. **Gebruik het bestaande filter** in plaats van afkappen, of leg uit waarom dat hier niet kan. Twee manieren om een datum te tonen in één applicatie lopen uit de pas, en dat is precies hoe dit is ontstaan.
3. **Geef de kolommen een verdeling die bij hun inhoud past.** Meet de werkelijke breedtes in de browser; ga niet op gevoel schuiven. Let erop dat de tabel op een smal scherm ook nog werkt.
4. **Kijk of dit elders ook speelt.** Zoek naar andere `[:16]`-afkappingen en naar andere tabellen met een datumkolom; als de takentabel de enige is, zeg dat, want dan is de reparatie klaar.

## Verifieerbaar

- De datum staat op één regel, gemeten in de browser bij de gangbare breedtes.
- De getoonde tijd van een taak komt overeen met het tijdstip waarop die taak volgens de log draaide, met de omrekening erbij uitgelegd in de PR.
- `uv run pytest tests/ -q` groen, plus `ruff check`, `ruff format`, `pyright`.
