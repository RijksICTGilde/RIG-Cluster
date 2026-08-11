# ROOS eruit

Status: plan, 11 augustus 2026. Aanleiding: er wordt overgestapt op het nieuwe componentensysteem, niet parallel gedraaid. De brug tussen de twee is met RC-65 al verdwenen; wat blijft staan is de oude weg zelf. Zolang die bestaat is elke wijziging twee keer werk en lopen de twee uit de pas, en dat is vandaag al drie keer gebeurd.

## Wat er nu is, gemeten

| | |
|---|---|
| sjablonen in `opi/templates/` (roos) | **155** |
| sjablonen in `opi/templates_lotc/` | **208** |
| `render()`-aanroepen met een `roos=` | **39** |
| **daarvan zonder `lotc=`-tegenhanger** | **0** |
| `opi/core/templates.py` (de roos-omgeving) | 304 regels |
| modules die `jinja_roos_components` aanroepen | `server.py`, `core/templates.py`, `utils/logging_config.py` |
| standaardweergave | de nieuwe (`DEFAULT_LAYOUT = LAYOUT_LOTC`) |

**Die nul is het belangrijkste getal van dit plan.** Elke pagina die vandaag een roos-sjabloon rendert, heeft er een LOTC-sjabloon naast. Er is dus niets meer om te bouwen; dit is opruimen, en dat maakt het een taak die af kan in plaats van een migratie die blijft duren.

## Wat eruit gaat

1. **De 155 sjablonen** in `opi/templates/`.
2. **De schakelaar.** `opi/web/lotc_switch.py`: `chosen_layout`, `wants_lotc`, `DEFAULT_LAYOUT`, de `?layout=`-parameter en het `zad_layout`-koekje. De 39 `render(roos=..., lotc=...)`-aanroepen worden een gewone render van het LOTC-sjabloon.
3. **De roos-omgeving**, `opi/core/templates.py`, en wat daarop leunt.
4. **De afhankelijkheid** `jinja-roos-components` uit `pyproject.toml` en `uv.lock`.
5. **De statische assets** van roos, en de `<link>`s ernaar.
6. **De tests die de twee vergelijken.** `test_lotc_parity.py`, `test_lotc_pariteit.py` en het pariteitsdeel van `test_lotc_project_tab.py` meten de nieuwe pagina tegen de oude. Zonder oude pagina meten ze niets meer.

## De volgorde, en waarom die bindend is

**Eerst de aanroepers, dan de sjablonen, dan de omgeving, dan de afhankelijkheid.** Andersom sloop je de grond onder je voeten weg en weet je bij de eerste rode test niet meer of het aan de sloop ligt of aan iets anders.

Draai na elke stap beide suites. Een stap die rood is, hoort gerepareerd te zijn voordat de volgende begint; dit is precies het soort werk waarbij vijf halve stappen samen onvindbaar worden.

## De tests zijn hier het lastigste deel

De pariteitstests zijn de reden dat deze omzetting zo ver gekomen is: ze vergeleken elke knop, elk endpoint en elk veld van de oude pagina met de nieuwe. Ze weggooien voelt als achteruitgang, en dat is het ook, maar ze meten straks de ene helft van niets tegen de andere.

**Zet er iets voor terug dat wél iets meet.** Een test die vastlegt dat een pagina zijn knoppen, zijn endpoints en zijn velden heeft, met de LIJST erin in plaats van met de oude pagina als bron. Dat is minder elegant en het veroudert, maar het is eerlijk: nu is de oude pagina de norm, straks is de nieuwe het.

Loop ze een voor een langs en beslis per test: vervalt hij, of gaat hij mee met de nieuwe pagina als onderwerp. Beide zijn goede uitkomsten; stilletjes verdwijnen is de enige slechte.

## Waar op te letten

**Een sjabloon dat niemand rendert is niet automatisch dood.** Vandaag bleek `bg/project-details.html.j2` te bestaan zonder dat iemand het rendert. Zulke wezen ontstaan juist bij een omzetting, en bij een sloop haal je ze weg zonder het te merken; dat is prima, maar controleer of er niets omheen hangt.

**De statische assets zijn niet alleen roos.** `static/css/` bevat bestanden die de nieuwe schil ook laadt: `wizard.css` voor de hulpdialoog van een dienst, `base.css` om precies één klasse (`is-hidden`), `modal.css` voor de gedeelde dialoog. Die staan er met reden en met een aantekening erbij. Verwijder alleen wat aantoonbaar alleen door roos gebruikt werd.

**De klassen die geen vormgeving zijn blijven.** `config-item`, `config-code`, `copy-btn`, `deployment-section`, `is-hidden`: daar hangt JavaScript aan. Ze zien eruit als opmaak en zijn het niet.

**Kijk ernaar als je klaar bent.** Niet alleen groene tests: loop de pagina's en de dialogen langs met beeld. Vandaag zijn er vijf dingen gevonden terwijl de suite groen stond, waaronder een voettekst die in de inhoudskolom hing en een menu dat wel in de DOM stond maar niet openging. Een sloop van deze omvang verdient diezelfde controle.

**Wat je niet moet doen: en passant verbeteren.** De verleiding is groot om bij het omzetten van een `render()` ook even de pagina op te knappen. Doe dat niet in deze taak: dan is bij een rode test niet meer te zien of het door de sloop komt.
