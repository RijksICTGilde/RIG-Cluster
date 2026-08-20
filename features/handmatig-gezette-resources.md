# Een handmatig gezette resource-waarde wint van de auto-tuner

Zet je via de portal of de API het geheugen of de CPU van een component, dan is dat
vanaf nu de waarde die draait -- ook als de auto-tuner dat component al eens heeft
bijgesteld. De tuner laat precies de velden die jij zette met rust tot ze vervallen, en
tunet de rest gewoon door.

## Waarom dit er is

Er waren twee schrijvers die naar twee verschillende niveaus schreven:

| schrijver | schrijft naar |
|---|---|
| portal "Component bewerken" en de API (`add_component` / `update_component`) | `components[]` -- de catalogus |
| auto-tuner en OOM-watcher | `deployments[].components[].resources` -- de werkkopie |

Manifestgeneratie leest eerst de catalogus en legt de werkkopie er daarna overheen. Wie
via de portal iets zette schreef dus naar het niveau dat verliest. Op `mpfb-8wh` werd op
19 augustus 2026 de CPU van een component van `32m`/`200m` naar `50m`/`1` gezet; de
wijziging is netjes gecommit, en de pods bleven op de oude waarden draaien. Zodra de
tuner een component ooit had aangeraakt was elke bewerking daarna een stille no-op.

## Hoe het werkt

Alle gebruikersgerichte schrijvers lopen nu via één functie,
`ProjectFileHandler.apply_user_resource_intent`. Die doet drie dingen in deze volgorde:

1. **De gewijzigde velden in de catalogus schrijven.** Alleen velden die echt afwijken van
   wat er stond. Dat is essentieel: de bewerkmodal post alle vier de resource-velden bij
   elke opslag, dus zonder die vergelijking zou elke willekeurige componentbewerking alle
   vier de velden vastzetten en de tuner volledig lamleggen.
2. **Precies die velden uit elke deployment-override halen.** Niet het hele blok: een
   CPU-bewerking laat de door de tuner gezette geheugenwaarde van die deployments staan.
   De historie van de tuner blijft ook staan. Met één correctie erop: zie
   "Een paar gaat altijd in zijn geheel" hieronder.
3. **De wens vastleggen** als één item in de historie van de catalogus-component:

   ```yaml
   history:
     - timestamp: '2026-08-20T09:03:53.493412+00:00'
       source: manual
       limits:
         cpu: '1'
       requests:
         cpu: 50m
       reason: 'Set by hand via portal: limits.cpu -> 1, requests.cpu -> 50m'
   ```

   Geen `deployment`-veld: de wens geldt voor elk deployment van dit component. De bron
   `manual` stond al in het schema (`$defs/resource-history-entry`) en werd tot nu toe door
   niemand geschreven -- er is dus geen schemawijziging en geen migratie.

Wijzigt een bewerking niets, dan gebeurt er ook niets: geen historie-item, geen
commit-ruis.

### Een paar gaat altijd in zijn geheel

Manifestgeneratie merget de catalogus en de deployment-override **per veld**. Haal je dan
één helft van een paar uit de override, dan wordt de andere helft van de tuner gepaard met
de catalogus -- en dat kan `requests > limits` opleveren, wat de apiserver weigert. Precies
na de bewerking faalt dan de ArgoCD-sync.

Het is de gewone weg, geen randgeval: de modal toont de **catalogus**-waarde, dus wat de
tuner op deploymentniveau als request heeft staan is voor wie de bewerking doet niet eens
zichtbaar. Catalogus `64Mi`/`256Mi`, tuner-override `600Mi`/`900Mi`, jij zet de
geheugenlimiet op `512Mi` -- effectief `requests 600Mi` bij `limits 512Mi`.

Daarom wordt na het opruimen per deployment het gemergde paar getoetst, voor geheugen en
voor CPU. Staat de request boven de limiet, dan gaat de andere helft ook uit de override:
het hele paar komt weer uit de catalogus, en dat paar is bij binnenkomst al bewaakt (de
sectie-enforcer voor geheugen, de toegestane waarden voor CPU).

### Het nieuwste item draagt de hele staande wens

Een bewerking raakt meestal maar één veld, maar de wens gaat over het component als
geheel. De lezer neemt per niveau precies het **nieuwste** `manual`-item, dus een item dat
alleen de velden van die ene bewerking draagt zou de wens van de vorige bewerking stil
laten vallen: zet je maandag de CPU vast en dinsdag het geheugen, dan mag de tuner die CPU
dinsdagnacht weer verzetten. Precies de stille no-op die dit schrijfpad moet voorkomen,
één bewerking later terug.

Daarom neemt elk nieuw item de nog **staande** velden van het vorige item mee: elk veld dat
deze bewerking niet zelf overschrijft en waarvan de waarde nog in de catalogus staat. Die
velden worden ook uit de deployment-overrides gehaald, want een wens die het manifest niet
haalt is geen wens. In de `reason` staan ze apart genoemd:

```
Set by hand via portal: limits.memory -> 900Mi; still standing: limits.cpu -> 1
```

Drie gevolgen om te kennen:

- Een veld waarvan de waarde langs een andere weg is gewijzigd (bijvoorbeeld de
  sectiestroom over de hele componentenlijst, die geen wens vastlegt) staat niet meer en
  gaat niet mee.
- De vervaltermijn gaat voor die meegenomen velden opnieuw lopen: het nieuwe item heeft een
  nieuwe tijdstempel. Dat is verdedigbaar -- de modal post alle vier de velden, dus wie
  opslaat bevestigt de waarde die hij ziet -- maar het betekent dat iemand die het
  component regelmatig bewerkt een wens onbeperkt levend houdt.
- Eén override blijft er bij dat opruimen staan: een geheugenlimiet die de **OOM-watcher**
  boven de wens heeft getild. Dat is de enige plek waar het platform de gebruiker bewust
  overruled, en die verhoging wegvegen op een losstaande CPU-bewerking zet de pod terug op
  een limiet die al te klein bleek -- met request gelijk aan limiet, dus zonder ruimte om
  te pieken. Niets zou dat rechtzetten: zolang de wens leeft houdt de tuner de limiet waar
  hij staat, en `has_oom_kills` is pas weer waar als de pod opnieuw omvalt. Alleen een
  vloer **boven** de staande wens telt; of die vloer al vervallen is wordt hier niet
  beoordeeld (daar zijn de metingen van de tuner voor nodig), dus de override blijft staan
  en de tuner haalt hem omlaag bij de eerste sweep na het vervallen.

### De tuner tegenover een wens

`_analyze_component_resources` haalt de wens op met `get_user_resource_intent` en slaat
**exact de genoemde velden** over zolang die leeft. De rest van het component wordt gewoon
getuned. Elk veld dat daadwerkelijk een aanbeveling tegenhoudt wordt op INFO gelogd, met
de tijdstempel van het item, zodat in Loki terug te zien is waarom een waarde niet beweegt:

```
Skipping limits_cpu for api in productie: kept at 1 (user set it 2026-08-19T12:59:11+00:00),
recommendation was 250m
```

**Vervallen.** Een wens blijft niet eeuwig staan, anders kan een veel te ruim gezette
waarde (iemand zet 4Gi op iets dat 100Mi gebruikt) nooit meer worden rechtgezet. De regel
spiegelt die van de OOM-vloer: het item is ouder dan `user_intent_min_age_days` **en** het
gemeten gebruik ligt onder `user_intent_stable_percent` van wat er gezet is. Voor geheugen
is "gemeten gebruik" de waargenomen max, voor CPU de VPA-target. Zonder meting vervalt er
niets (dan blijft de bescherming staan).

**De ene uitzondering.** Bij een actieve OOM-kill mag de tuner de geheugen*limiet* wel
boven een levende wens tillen. Een pod die op dit moment omvalt is precies het geval
waarin de tuner moet ingrijpen; de bestaande OOM-noodroute blijft daarmee volledig intact.
Het geheugen-*request* en de CPU blijven ook dan staan.

**Invarianten.** Zet je maar één helft van een paar vast, dan geeft de andere helft mee:
met een vastgezette limiet wordt het request naar die limiet geknepen, met een vastgezet
request wordt de limiet opgehoogd zodat het past. Zijn beide vastgezet, dan is het paar
precies wat de gebruiker zette en blijft het onaangeroerd. Dezelfde invariant bewaakt het
schrijfpad bij het opruimen van de overrides (zie "Een paar gaat altijd in zijn geheel").

### Snoeien gooit de wens niet weg

De historie is een venster van vijf items. `_prune_resource_history` beschermde al het
nieuwste `oom-watcher`-item (dat is de OOM-vloer) en beschermt nu ook het nieuwste
`manual`-item. De beschermde items claimen hun slot vóórdat het venster met de nieuwste
overige items wordt volgemaakt; `max_entries` blijft hard. Die volgorde is nodig: wie de
redding als een vervanging achteraf doet, vindt in een venster dat volledig uit beschermde
items bestaat (een OOM-storm) geen vrij slot en gooit het item alsnog weg. `_compact_resource_history_list` vouwt
alleen runs van identieke `auto-tune`-items, dus een `manual`-item breekt zo'n run en
blijft vanzelf staan.

### De platformcap blijft de bovengrens

Een wens die de tuner met rust laat mag niet boven de per-component cap uitkomen
(`max_memory_limit_mi` 4096, `max_cpu_limit_m` 4000, uit `opi/core/cluster_config.py`). Die
cap werd tot nu toe op twee plaatsen afgedwongen: door de veldregels op het formulier en op
de aanmaakweg van de API, en door de tuner, die een te ruime waarde bij de eerstvolgende
sweep terugklemde met een deployment-override. Het bijwerkprofiel van de API
(`UPDATE_COMPONENT_VALIDATORS`) toetste geen van beide limieten, dus
`PATCH /api/projects/{p}/components/{c}` met `{"memory_limit": "64Gi"}` ging erdoor en werd
's nachts stilletjes rechtgezet.

Dit mechanisme haalt die tweede lijn weg -- en een pin boven de cap loopt ook niet vanzelf
af, want vervallen eist ouderdom **en** gebruik onder de helft van de gezette waarde: een
workload die zijn eigen pin vol houdt, houdt hem onbeperkt. Daarom toetst het bijwerkprofiel
`cpu_limit` en `memory_limit` nu met dezelfde twee veldregels als de aanmaakweg. Een waarde
boven de cap levert een 422 met dezelfde melding als het formulier, en er wordt niets
geschreven: geen catalogus-waarde, geen opgeruimde override, geen `manual`-item. Er is geen
derde lijn achter deze -- er staat geen `ResourceQuota` of `LimitRange` in `manifests/`, en
het projectschema typeert `resources` kaal als string.

## Configuratie

Naast de OOM-vloervelden, in `opi/services/catalog/resource_tuning/config.py`:

| Veld | Standaard | Betekenis |
|---|---|---|
| `user_intent_min_age_days` | `10` | Een handmatig gezette waarde kan pas na zoveel dagen vervallen |
| `user_intent_stable_percent` | `50` | ...en alleen als het gemeten gebruik onder dit percentage van die waarde blijft |

De startwaarden zijn gelijk aan die van de OOM-vloer, zodat het gedrag in één zin uit te
leggen is; ze staan los zodat ze apart bij te draaien zijn.

## Wat dit niet doet

- **Terugwerkend werken.** Waarden die vóór deze wijziging met de hand zijn gezet hebben
  geen `manual`-item en krijgen dus pas bescherming bij de volgende bewerking. Raden naar
  historische intentie is precies wat we hier niet willen.
- **De API uitbreiden.** Die kent alleen `cpu_limit` en `memory_limit`, geen requests. De
  gedeelde functie accepteert partiële invoer, dus dat kan zo blijven.
- **Tonen dat een waarde vaststaat.** Er is nog geen scherm dat laat zien dat een veld
  vastligt, en geen knop om de wens los te laten. Je raakt hem kwijt door hem te
  overschrijven, door hem te laten vervallen, of door de waarde langs een weg te wijzigen
  die geen wens vastlegt (zie hierboven).
- **De CPU-freeze vervangen.** `compute_cpu_recommendation` leidt intentie nog steeds af
  uit `limit != request` voor componenten zónder vastgelegde wens. Die heuristiek raadt,
  maar blunt weghalen zou de tuner in één nachtelijke sweep de limiet van zo goed als elk
  productiecomponent laten gelijktrekken met zijn request. Hij is nu expliciet de
  terugval, en kan weg zodra de wens breed is vastgelegd.

## Belangrijke bestanden

| Bestand | Rol |
|---|---|
| `opi/handlers/project_file_handler.py` | `apply_user_resource_intent` (het ene schrijfpad), `get_user_resource_intent`, `_prune_resource_history` |
| `opi/api/validation.py` | `UPDATE_COMPONENT_VALIDATORS` toetst de twee limieten tegen de platformcap, met dezelfde veldregels als de aanmaakweg |
| `opi/forms/wizard/save.py` | De portal-bewerking haakt hierin na de merge, met de vorige waarden voor de vergelijking |
| `opi/manager/project_manager.py` | `update_component` schrijft via hetzelfde pad |
| `opi/utils/project_utils.py` | `build_component_config` (nieuw component) idem; `apply_resource_limits` is nog de lage schrijver van de geneste vorm |
| `opi/services/resource_tuning_service.py` | `_live_intent_fields`, `_honour_user_intent`, de vervalregel |
| `tests/test_gebruikerswens_resources.py` | De keten van bewerking tot manifest, plus de grendel op het ene schrijfpad |

## Verwant

- `features/auto-resource-tuning.md` -- de tuner zelf
- `features/oom-kill-watcher.md` -- de watcher die de OOM-vloer zet
