# Een handmatig gezette resource-waarde wint van de auto-tuner

Zet je via de portal of de API het geheugen of de CPU van een component, dan is dat
vanaf nu de waarde die draait -- ook als de auto-tuner dat component al eens heeft
bijgesteld. De tuner laat precies de velden die jij zette met rust tot jij het vastzetten
opheft, en tunet de rest gewoon door. Dat vastzetten is de ENIGE uitzondering op de regel
dat de tuner alles bijstelt (RC-518); de waarde die op het component staat is dat niet.

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
- De tijdstempel van die meegenomen velden loopt opnieuw. Dat verandert niets aan wat
  vastligt (een wens vervalt niet), alleen aan de datum die de portal toont.
- Eén override blijft er bij dat opruimen staan: een geheugenlimiet die de **OOM-watcher**
  boven de wens heeft getild. Dat is de enige plek waar het platform de gebruiker bewust
  overruled, en die verhoging wegvegen op een losstaande CPU-bewerking zet de pod terug op
  een limiet die al te klein bleek -- met request gelijk aan limiet, dus zonder ruimte om
  te pieken. Niets zou dat rechtzetten: zolang de wens staat houdt de tuner de limiet waar
  hij staat, en `has_oom_kills` is pas weer waar als de pod opnieuw omvalt. Alleen een
  vloer **boven** de staande wens telt; of die vloer al vervallen is wordt hier niet
  beoordeeld (daar zijn de metingen van de tuner voor nodig), dus de override blijft staan
  en de tuner haalt hem omlaag bij de eerste sweep nadat de VLOER vervalt.

### Een nieuw component legt geen wens vast

De aanmaakweg (`build_component_config`, gebruikt door de zelfbedieningswizard en door
`POST .../components`) schrijft de twee limieten **rechtstreeks** in de geneste vorm, niet
via `apply_user_resource_intent`. Dat is een bewuste uitzondering op "één schrijver".

Een aanmaakverzoek draagt namelijk altijd twee limieten: de wizard vult 1 CPU en 256Mi
voor, en de API valt op dezelfde waarden terug. Liep die weg door het gedeelde pad, dan
kreeg **elk** nieuw component meteen een `manual`-item -- en dus een limiet die de tuner
nooit meer mocht verzetten, met `requests.memory` gelijk aan `limits.memory` en dus zonder
piekruimte (precies waar `min_limit_headroom_mi` voor is). Sinds RC-518 vervalt een wens
ook niet meer vanzelf, dus zo'n component zou zijn 256Mi houden tot iemand hem met de hand
loslaat.

Een voorgevulde standaard die niemand heeft gekozen is geen wens. De eerste echte
bewerking van het component is dat wel, en die loopt nog steeds via het gedeelde pad. Er
gaat bij het rechtstreeks schrijven ook niets verloren: een component dat nog niet bestaat
heeft geen deployment-override om op te ruimen en geen historie om mee te dragen. De
grendel op het ene schrijfpad noemt `build_component_config` daarom expliciet als de enige
andere toegestane aanroeper van `apply_resource_limits`.

### De tuner tegenover een wens

`_analyze_component_resources` haalt de wens op met `get_user_resource_intent` en slaat
**exact de genoemde velden** over zolang ze vastgezet zijn. De rest van het component wordt gewoon
getuned. Elk veld dat daadwerkelijk een aanbeveling tegenhoudt wordt op INFO gelogd, met
de tijdstempel van het item, zodat in Loki terug te zien is waarom een waarde niet beweegt:

```
Skipping limits_cpu for api in productie: kept at 1 (user set it 2026-08-19T12:59:11+00:00),
recommendation was 250m
```

**Vastgezet blijft vastgezet.** Tot RC-518 verviel een wens na tien dagen als het gebruik
onder de helft van de gezette waarde bleef. Dat strookt niet met wat vastzetten betekent:
"dit heb ik echt nodig, wat de tuner ook meet". De vervalregel is weg, inclusief
`user_intent_min_age_days` en `user_intent_stable_percent`. Een veel te ruim gezette waarde
blijft dus staan tot iemand hem loslaat, en de portal laat per veld zien wie hem beheert,
zodat dat te zien en te doen is.

**De ene uitzondering.** Bij een actieve OOM-kill mag de tuner de geheugen*limiet* wel
boven een vastgezette wens tillen. Een pod die op dit moment omvalt is precies het geval
waarin de tuner moet ingrijpen; de bestaande OOM-noodroute blijft daarmee volledig intact.
Het geheugen-*request* en de CPU blijven ook dan staan.

**Invarianten.** Zet je maar één helft van een paar vast, dan geeft de andere helft mee:
met een vastgezette limiet wordt het request naar die limiet geknepen, met een vastgezet
request wordt de limiet opgehoogd zodat het past. Zijn beide vastgezet, dan is het paar
precies wat de gebruiker zette en blijft het onaangeroerd. Dezelfde invariant bewaakt het
schrijfpad bij het opruimen van de overrides (zie "Een paar gaat altijd in zijn geheel").

### Het vastzetten opheffen

Een vastgezet veld geef je terug aan de tuner met de knop **"Laat de tuner dit weer
regelen"** op het tabblad Componenten. Die loopt via
`ProjectFileHandler.release_user_resource_intent`, dat een nieuw `manual`-item schrijft met
de staande wens **minus** het losgelaten veld:

```yaml
history:
  - timestamp: '2026-11-22T13:04:11.002931+00:00'
    source: manual
    limits:
      cpu: '1'
    reason: 'Released via portal, the tuner manages it again: limits.memory; still standing: limits.cpu -> 1'
```

Omdat de lezer alleen het NIEUWSTE item neemt, is dat item de hele uitspraak: wat er niet
in staat is niet vastgezet. Blijft er niets over, dan draagt het item geen resourceveld
meer en leest `get_user_resource_intent` het als "niets vastgezet". De waarde zelf
verandert niet; alleen wie hem vanaf nu mag verzetten.

### Wat de portal laat zien

Op het tabblad Componenten staat per component een regel per resourceveld (request en
limiet, geheugen en CPU) met de waarde en, als hij vastgezet is, de datum waarop dat
gebeurde plus de knop hierboven. Boven de lijst staat eenmaal dat het platform de rest
bijstelt op het gebruik.

De getoonde waarden komen uit `extract_component_resources` en
`get_component_resource_pins`, dezelfde twee lezers die de tuner gebruikt. Daardoor kunnen
scherm en tuner niet uit elkaar lopen, en toont het scherm bij de oude vorm
(`resources: {cpu, memory}` zonder `requests`/`limits`) de waarde die ECHT draait: die vorm
wordt door manifestgeneratie en tuner genegeerd en valt terug op de platformstandaarden.

### De gevallen van voor 20 augustus 2026

Wie vóór RC-141 na een OOM-kill het geheugen van zijn component met de hand ophoogde, heeft
geen `manual`-item: er was toen niets dat zo'n wijziging vastlegde. Zolang de waarde op het
component een ondergrens was, was dat niet erg. Sinds RC-518 is die ondergrens weg, dus
zonder actie zou de tuner die componenten weer verlagen en vallen ze opnieuw om.

`opi/tools/resource_pin_migration.py` legt die gevallen eenmalig vast. Een los script en
geen schemamigratie: het besluit rust op de GIT-HISTORIE van het projectbestand, en een
schemamigratie ziet alleen een geparseerde dict op het moment van inlezen, per bestand, bij
elke lees. Het moet ook eenmalig en bewust per cluster draaien, met de lijst door een mens
gelezen voordat er iets geschreven wordt.

```bash
uv run python -m opi.tools.resource_pin_migration            # dry-run, toont de lijst
uv run python -m opi.tools.resource_pin_migration --write    # legt de vastzettingen vast
```

Het besluit per component: lopend door de historie van zijn projectbestand, van nieuw naar
oud, is de eerste commit die zijn gedeclareerde resources wijzigde en NIET van het platform
kwam een wijziging met de hand. Een commit is van het platform als het bericht met
`auto-tune:` of `sanitize:` begint, of als de resource-historie van dat component in diezelfde
commit groeide (wat de tuner schreef toen hij het component zelf nog aanraakte). De velden
die daar wijzigden worden vastgezet, op de waarde die vandaag staat. Een component met een
`manual`-item wordt overgeslagen, en de commit waarin een component ONTSTAAT telt niet: een
aanmaakwaarde is geen wens.

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
af, want een pin vervalt niet. Daarom toetst het bijwerkprofiel
`cpu_limit` en `memory_limit` nu met dezelfde twee veldregels als de aanmaakweg. Een waarde
boven de cap levert een 422 met dezelfde melding als het formulier, en er wordt niets
geschreven: geen catalogus-waarde, geen opgeruimde override, geen `manual`-item. Er is geen
derde lijn achter deze -- er staat geen `ResourceQuota` of `LimitRange` in `manifests/`, en
het projectschema typeert `resources` kaal als string.

## Configuratie

Geen. Een vastgezet veld blijft vastgezet tot de gebruiker het loslaat, dus er is geen
termijn en geen drempel om bij te draaien. De twee velden die dat tot RC-518 deden
(`user_intent_min_age_days` en `user_intent_stable_percent`) zijn uit
`opi/services/catalog/resource_tuning/config.py` verdwenen.

## Wat dit niet doet

- **Terugwerkend werken.** Waarden die vóór deze wijziging met de hand zijn gezet hebben
  geen `manual`-item en krijgen dus pas bescherming bij de volgende bewerking. Raden naar
  historische intentie is precies wat we hier niet willen.
- **De API uitbreiden.** Die kent alleen `cpu_limit` en `memory_limit`, geen requests. De
  gedeelde functie accepteert partiële invoer, dus dat kan zo blijven.
- **De wens aan een deployment hangen.** Een wens geldt voor het component en dus voor al
  zijn deployments; per deployment iets anders vastzetten kan niet via dit pad.
- **De CPU-freeze vervangen.** `compute_cpu_recommendation` leidt intentie nog steeds af
  uit `limit != request` voor componenten zónder vastgelegde wens. Die heuristiek raadt,
  maar blunt weghalen zou de tuner in één nachtelijke sweep de limiet van zo goed als elk
  productiecomponent laten gelijktrekken met zijn request. Hij is nu expliciet de
  terugval, en kan weg zodra de wens breed is vastgelegd.

## Belangrijke bestanden

| Bestand | Rol |
|---|---|
| `opi/services/project_file_handler.py` | `apply_user_resource_intent` (het ene schrijfpad), `release_user_resource_intent`, `pin_resource_fields`, `get_user_resource_intent`, `get_component_resource_pins`, `_prune_resource_history` |
| `opi/http/render/lotc_switch.py` | `build_component_resource_rows`: per veld de waarde en wie hem beheert |
| `opi/templates_lotc/bg/_component-resources.html.j2` | Dat lijstje op het tabblad Componenten, met de knop om los te laten |
| `opi/tools/resource_pin_migration.py` | De eenmalige vastlegging van de gevallen van voor 20 augustus 2026 |
| `opi/api/validation.py` | `UPDATE_COMPONENT_VALIDATORS` toetst de twee limieten tegen de platformcap, met dezelfde veldregels als de aanmaakweg |
| `opi/forms/wizard/save.py` | De portal-bewerking haakt hierin na de merge, met de vorige waarden voor de vergelijking |
| `opi/manager/project_manager.py` | `update_component` schrijft via hetzelfde pad |
| `opi/services/project_utils.py` | `apply_resource_limits` (de lage schrijver van de geneste vorm); `build_component_config` schrijft daar rechtstreeks in en legt bij aanmaak geen wens vast |
| `opi/services/resource_tuning_service.py` | `_live_intent_fields`, `_honour_user_intent` |
| `tests/test_gebruikerswens_resources.py` | De keten van bewerking tot manifest, plus de grendel op het ene schrijfpad |

## Verwant

- `features/auto-resource-tuning.md` -- de tuner zelf
- `features/oom-kill-watcher.md` -- de watcher die de OOM-vloer zet
