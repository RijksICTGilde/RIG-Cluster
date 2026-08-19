# Een schemafout die niemand ziet, en pas opvalt als iemand toevallig in de logs kijkt

In de nacht van 17 op 18 augustus stonden er om 01:00 vijfentwintig waarschuwingen in de productielog, een per project:

```
Persisting project 'algor-1ha' despite a validation failure (enforce_validation=False);
the project file has pre-existing drift that should be repaired:
Veld 'deployments/0/components/1/resources/history/0':
Additional properties are not allowed ('requests' was unexpected)
```

01:00 is precies wanneer de resource-tuner draait. Die schrijft in de historie zowel `limits` als `requests`, met een goede reden in `opi/services/resource_tuning_service.py:689`: een wijziging die alleen de request verplaatst zou anders als een no-op lezen. Maar `resource-history-entry` in `opi/schemas/project_v2.json` kende alleen `limits`, met `additionalProperties: false`.

Het schemagat zelf is gerepareerd (`c0fc23d1`). **Deze taak gaat niet over dat gat maar over de vraag waarom het nooit opviel**, en of de validatie op de goede plek zit.

## Wat er gemeten is

**Validatie gebeurt alleen bij SCHRIJVEN.** `validate_project_schema` wordt op vijf plekken aangeroepen, en dat zijn allemaal schrijfpaden: `opi/web/router_wizard.py:2254`, `opi/manager/project_manager.py:2826`, en `opi/services/project_store.py:689` en `:709`. Het inlezen doet alleen `migrate_to_latest` en valideert niets. Een ongeldig bestand wordt dus gewoon ingelezen, getoond en gebruikt.

**En dat schrijfpad kan de fout degraderen.** `ProjectStore._validate(data, enforce=...)` vangt `ProjectSchemaError` en logt hem als waarschuwing in plaats van hem te werpen.

**Dat laatste is een bewuste keuze en die moet blijven.** De omgekeerde keuze hebben we in juni gehad met het registry-gat (`f071f10d`): daar werd hard gevalideerd, en één schemafout blokkeerde stil álle deploys van dat project. Dat is erger. **Deze taak mag dat gedrag niet omdraaien: projecten blijven werken ondanks een fout in hun bestand.**

De prijs is wat er nu gebeurt: 25 projectbestanden zijn formeel ongeldig, het platform draait vrolijk door, en het enige signaal is een logregel die iemand moet zien langskomen. Het is opgemerkt doordat de opdrachtgever toevallig in de logs keek.

## De vraag die deze taak moet beantwoorden

**Waarom vangt niets dit af voordat het in 25 productiebestanden staat?**

Er is een sterke kandidaat, en die is het onderzoeken waard voordat je iets bouwt: er is geen enkele test die valideert wat de CODE SCHRIJFT tegen het SCHEMA. De tuner heeft tests, het schema heeft tests, maar niemand legt het door de tuner geproduceerde historie-item langs `validate_project_schema`. Precies datzelfde gold in juni voor de registry-velden. Twee keer dezelfde vorm is geen toeval maar een gat in de opzet.

Onderzoek daarom eerst: **welke plekken in de code schrijven structuur in het projectbestand, en welke daarvan worden ooit tegen het schema gevalideerd in een test?** Denk aan de resource-tuner, de registries, keycloak, invite, attachments, sleep-mode-state, backup-generaties, de domeinaanvragen. Lever die lijst op; hij is op zichzelf al waardevol, ook als de reparatie daarna klein blijkt.

## Richtingen, met hun prijs

**A. Een test die elke schrijver langs het schema legt.** Dit is de kandidaat die de oorzaak raakt: had die bestaan, dan was dit in CI gevallen in plaats van in productie. De vraag is hoe je dat opzet zonder voor elke schrijver met de hand een voorbeeld te verzinnen dat daarna veroudert. Kijk of de bestaande tests van elke schrijver hun resultaat al in handen hebben, zodat er alleen een validatie-assertie bij hoeft.

**B. Een ongeldig projectbestand zichtbaar maken waar mensen kijken.** Nu gaat de melding alleen naar de log. Een project dat zijn eigen schema niet haalt is een feit dat op de projectpagina of in het beheeroverzicht thuishoort, en via de API opvraagbaar. Prijs: je moet ergens onthouden dat het zo is, want bij lezen wordt er niet gevalideerd, en je wilt niet bij elke paginaweergave het hele schema draaien.

**C. Een periodieke controle over alle projectbestanden**, die rapporteert wat er niet valideert. Sluit aan bij de nachtelijke reconciliatie die er sinds `8775885f` is. Prijs: het vindt het pas achteraf, niet bij het schrijven.

A en B vullen elkaar aan: A voorkomt nieuwe gaten, B maakt bestaande zichtbaar. C is een vangnet voor bestanden die van buitenaf zijn aangepast. Kies met een reden; alle drie hoeft niet.

## Wat er buiten valt

- **De validatie hard maken.** Expliciet niet. Projecten blijven werken ondanks een fout in hun bestand; dat is in juni duur geleerd.
- Het `requests`-gat zelf, dat is al gerepareerd.
- Het herstellen van de 25 bestanden: die zijn met de schemafix vanzelf weer geldig, er is geen migratie nodig.

## Verifieerbaar

- De lijst uit "de vraag die deze taak moet beantwoorden": welke schrijvers er zijn en welke ooit tegen het schema gevalideerd worden.
- Een test die omvalt als je de `requests`-toevoeging uit `project_v2.json` weghaalt, maar dan vanuit de KANT VAN DE TUNER in plaats van vanuit een handgeschreven voorbeeld. Dat is het bewijs dat de nieuwe opzet dit gevangen zou hebben.
- Blijkt er nog een schrijver te zijn met hetzelfde gat, dan is dat een vondst en hoort hij in het verslag, met of zonder reparatie.
- `uv run pytest tests/ -q` groen, plus `ruff check`, `ruff format`, `pyright`.
