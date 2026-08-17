# De samenvatting kan geen markup meer bouwen

Status: plan, 7 augustus 2026. Niet gebouwd. Aanleiding: twee punten die de reviewer van RC-47 als niet-blokkerend meldde. Beide zijn nagemeten en beide kloppen.

RC-47 heeft de stored XSS in de wizardsamenvatting gedicht: elke f-string die HTML bouwt gaat door `_summary_text`, ook de vier plekken in `_build_sequence_summary`. Wat overblijft zijn twee gaten in dezelfde muur.

## Wat er nog open staat, gemeten

**1. De `service_cards`-tak slaat `_format_value` over.** In `_build_section_fields` heeft die tak een eigen pad via `_resolve_service_labels`; de waarde komt daar binnen zonder langs `_format_value` te gaan. Gevolg: een `summarizer` op zo'n editable geldt daar niet, terwijl de feature-documentatie stelt dat beide bouwers door `_format_value` lopen. Vandaag onschadelijk (de labels komen uit de dienstencatalogus, niet uit gebruikersinvoer), maar het is een contract dat niet waar is, en dat is precies hoe de volgende fout ontstaat.

**2. Vier `summary_fn`-implementaties bouwen HTML met f-strings.** In `wizard_sections.py`: `_backup_summary`, `_restore_select_summary`, `_restore_target_summary` en `_new_deployment_summary`. Ze eindigen in dezelfde `| safe`-sink als de rest. `_backup_summary` is het duidelijkste geval:

```python
dep = data.get("deployment_name", "-")
return f"<p><strong>Deployment:</strong> {dep}</p><p><strong>Resource types:</strong> {types_str}</p>"
```

**Hoe erg is het, eerlijk.** `BACKUP_DEPLOYMENT_NAME_EDITABLE` heeft geen validator, dus een geknutselde POST komt hier ongefilterd door. Maar het veld is `transient=True` (het wordt niet opgeslagen) en het wordt gevoed uit een keuzelijst van bestaande deployments, die zelf uit het schemagevalideerde projectbestand komen. In de praktijk is dit dus self-XSS: je moet het bij jezelf naar binnen werken. Dat is de reden dat de reviewer het niet-blokkerend noemde, en die inschatting deel ik.

Het is toch de moeite waard, om twee redenen. Het is dezelfde klasse als de fout die RC-47 net dichtte, en de reparatie is goedkoop. En zolang deze vier bestaan, is "de samenvatting escapet" geen eigenschap van het systeem maar een gewoonte die iedereen moet onthouden.

## Voorstel

1. **Haal het `service_cards`-pad door dezelfde poort.** Of het gaat door `_format_value`, of de documentatie zegt eerlijk dat er twee paden zijn. Het eerste heeft de voorkeur, want dan geldt een `summarizer` overal.

2. **Laat een `summary_fn` geen HTML meer teruggeven.** Dit is de kern. Nu geeft hij een string die de sjabloon met `| safe` uitstoot, en dan is escapen een regel die je moet onthouden. Laat hem in plaats daarvan gegevens teruggeven (labels en waarden), en laat de sjabloon of de bestaande bouwer daar HTML van maken, geëscaped. Daarmee wordt escapen een eigenschap van de weg en niet van de schrijver.

3. **Een test die de `| safe`-sinks afdekt.** Faalt op een `summary_fn` die HTML-tekens teruggeeft, en faalt op een nieuwe f-string met een `<` in de summarybouwers. RC-47 heeft die tests voor zijn eigen deel; trek ze door.

## Volgorde

1. De vier `summary_fn`-implementaties omzetten naar gegevens. Verifieerbaar: dezelfde payload die in RC-47 werd gebruikt komt ook via de backup- en restore-stappen geëscaped terug.
2. Het `service_cards`-pad, met een test die aantoont dat een summarizer daar nu ook geldt.
3. De guard-test, als laatste, zodat hij op het opgeruimde bestand aanslaat.

## Waar op te letten

**Dit is geen spoedklus, en doe alsof het dat wel is zou de verkeerde les zijn.** De ernst is laag omdat de waarden transient en gevalideerd zijn. Wat het wel is: het laatste stuk van een muur die verder af is. Behandel het als hygiëne, met de rust die daarbij hoort.

**De winst zit in het weghalen van de keuze.** Zolang een `summary_fn` HTML mag teruggeven, kan de volgende die er een schrijft het fout doen, en dan is er niets dat hem tegenhoudt. Een functie die gegevens teruggeeft kán het niet fout doen.

**RC-47 heeft de patronen al staan.** `_summary_text` en de summarizer-opzet zijn er; dit plan breidt ze uit en verzint niets nieuws.
