# Twee event-lijsten, één manier van inhaken

Status: plan, 6 augustus 2026. Niet gebouwd. RC-36 is inmiddels gemerged, dus de blokkade is weg: de servicedefinitie staat nu in het servicepakket (nul `ServiceDefinition`-blokken in `services.py`) en dit plan bouwt daarop voort. Aanleiding: elke nieuwe uitbreiding vraagt nu een nieuwe methode op de basisklasse plus een nieuwe plek die de registry scant.

## De meting

```
29 publieke methoden op de Service-basisklasse
38 plekken in de code die over diensten itereren
 2 leden in de HookPoint-enum
```

Van de negenentwintig uitbreidingspunten lopen er dus twee via de enum. De rest is een methode met een eigen naam, die generieke code bij naam aanroept op een plek die daarvoor bedacht is. Zes van die haken hebben **één** bewoner (`config_approvals`, `contribute_deployment_manifests`, `deployment_page_sections`, `observe_deployment`, en de twee `deployment_component`-varianten); dat is maatwerk op de basisklasse, geen contract.

## De vorm, en waarom het bezwaar ertegen niet standhield

Het bezwaar tegen een generieke dispatch was dat je typecontrole verliest. Dat is gemeten en het klopt niet. Eén methode met een enum en een payload per event houdt de controle gewoon:

```python
class Service:
    @overload
    def handle(self, event: Literal[HookEvent.ROLLOUT], payload: RolloutPayload) -> None: ...
    @overload
    def handle(self, event: Literal[HookEvent.RENDER], payload: RenderPayload) -> str | None: ...
```

Een verkeerde payload levert dan:

```
error: No overloads for "handle" match the provided arguments
error: Argument of type "RenderPayload" cannot be assigned to parameter "payload" of type "RolloutPayload"
```

Sterker nog: dit levert **meer** controle op dan wat er nu staat. Dit project draait pyright met `reportCallIssue = false` en `reportArgumentType = false`, dus aanroepargumenten worden vandaag helemaal niet nagekeken. De 29 getypeerde methoden geven documentatie en editorhulp, geen afdwinging. Een payload-object is een echt type; losse argumenten die niemand controleert zijn dat niet.

(Dat die uitgezette controles het nakijken waard zijn, is een eigen punt en staat apart op de lijst.)

## Twee families, twee lijsten

Haken doen twee wezenlijk verschillende dingen, en die horen niet in één enum:

**UI-events: waar ben ik zichtbaar.** `detail_page_sections`, `deployment_page_sections`, `config_form_section`, de acties, de uitleg. Een dienst krijgt context en geeft iets terug om te tonen. Ze muteren niets en falen zichtbaar: een sectie die niets teruggeeft betekent gewoon geen sectie.

**Actie-events: wat doe ik als er iets gebeurt.** De rollout-haak uit RC-37, `observe_deployment`, de resource-tuning na een sync. Een dienst krijgt context en verandert de toestand. Deze hebben een contract dat de UI-kant niet heeft: **een actie-haak committeert niet zelf.** Hij muteert `ctx.project_data`, en de aanroeper doet ná de scan één `save_and_commit_project()` voor alle uitkomsten samen. Twee diensten die allebei committen geven twee commits en een lost-update-race.

Één enum voor allebei zou dat verschil verstoppen, en juist dat contract is het soort ding dat stilletjes sneuvelt. Dus twee lijsten, en de dispatch mag hetzelfde mechanisme zijn.

## Voorstel

1. **Twee enums**, `UIEvent` en `ActionEvent`, met per event een payload-type.
2. **Eén dispatch per familie**, met de enum als index: hier staat wie er luistert, in plaats van 38 plekken die zelf itereren.
3. **De zes eenmalige haken beoordelen**: verhuizen naar een event, of eruit omdat ze maatwerk zijn. Niet automatisch meenemen.
4. **De contracten blijven getypeerd** via overloads per event, zoals hierboven bewezen.

## Volgorde

1. De inventarisatie: welke van de 29 is een UI-event, welke een actie-event, en welke is geen van beide. Die uitkomst bepaalt of dit een grote of middelgrote klus is.
2. `ActionEvent` eerst, met de rollout-haak van RC-37 en `observe_deployment` als bewoners. Die familie is het kleinst en het contract is het scherpst.
3. `UIEvent` daarna, in groepen, met na elke groep de volledige suite groen.
4. De 38 scanplekken opruimen. Verifiëren: dat aantal daalt aantoonbaar, want dat is de hele opbrengst.

## Waar op te letten

**Dit is een verbouwing van het contract, geen opruiming.** Alles in `catalog/` hangt eraan. Doe het in groepen en houd de suite na elke groep groen, anders is een fout niet meer te herleiden naar een stap.

**Uniformiteit is geen doel op zich.** De 17 diensten op `config_editables` werken prima. De winst zit in de 38 scanplekken en de zes eenmalige haken, niet in het gelijkschakelen van wat al loopt.

**Een actie-haak committeert niet zelf.** Dit contract staat al in `plans/oom-auto-tune-deployment-scoped.md` en wordt met meer bewoners belangrijker, niet minder. Leg het vast op de `ActionEvent`-familie zelf, niet in een opmerking bij één haak.

**RC-38 loopt en raakt de API-kant van diensten.** Dat plan declareert acties bij de dienst zelf; deze verbouwt hoe diensten inhaken. Ze bijten elkaar niet, maar kijk bij het mergen naar `services/registry.py` en `catalog/base.py`, want daar komen ze samen.
