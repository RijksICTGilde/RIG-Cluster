# Reallife-doorloop 14 augustus 2026 (RC-112)

De `reallife`-suite draaide deze cyclus nog niet. Dat is de enige suite die vijf projecten
met semi-gelijktijdige mutaties via UI en API tegelijk doet, en daarmee het enige dat lijkt
op wat er in productie gebeurt. Alles wat we tot nu toe groen noemden, was gemeten met een
ding tegelijk.

Tweede doel: **punt 14** uit `plans/vragen-uit-zad-cli.md` -- de intermitterende
`deployment_not_found`.

## Wat er gedraaid is

Sandbox `kind-rig-sandbox`, eigen build, `/version` vijf keer achter elkaar gelijk aan de
commit onder toets. Twee volledige rondes, met de reparatie uit ronde 1 ertussen.

| Ronde | Commit | Suite | Uitkomst | Duur |
|---|---|---|---|---|
| 1 | `b1d42ffa` | `pytest -m reallife` | 7 passed, 1 xfailed, **0 rood** | 15m32s |
| 1 | `b1d42ffa` | `pytest -m punt14` (8 rondes x 2 vormen) | 3 passed, **0 rood** | 8m59s |
| 2 | `f56433a9` | `pytest -m reallife` | 7 passed, 1 xfailed, **0 rood** | 15m14s |
| 2 | `f56433a9` | `pytest -m punt14` (30 rondes x 2 vormen) | 3 passed, **0 rood** | 21m00s |
| 3 | `ec9ab51a` | `pytest -m reallife` | 7 passed, 1 xfailed, **0 rood** | 16m31s |
| 3 | `ec9ab51a` | `pytest -m punt14` (patches MET uitrol) | 5 rondes OK, daarna afgebroken op een 503 -- zie hieronder | |
| 3b | `ec9ab51a` | dezelfde variant, zonder tweede suite ernaast | zie hieronder | |

De twee suites liepen in ronde 2 en 3 **gelijktijdig**, zodat de punt-14-jacht werkelijke
belasting op de ProjectStore had en niet op een stil cluster meette.

## Punt 14: het oordeel

**Niet gereproduceerd, in ruim tachtig gerichte pogingen, waarvan het merendeel onder belasting.**

Dat is een expliciet "niet gereproduceerd", geen "het lijkt over". Wat er precies
geprobeerd is, zodat de volgende niet bij nul begint:

De toestand die de zad-cli beschrijft is nagebouwd: een project met drie componenten
(`web`, `alpha`, `beta`) en een stapel wijzigingen met `rollout=false` die per ronde
verder groeit (drie componentpatches per ronde). Daarna per ronde het paar dat bij hen
faalde: `:upsert-deployment` en meteen daarna `POST /deployments/<naam>/components`.

Drie vormen, in `tests/e2e/test_sandbox_punt14.py`:

1. **Wachtend** (wat de zad-cli doet): wacht tot de upserttaak KLAAR is, dan pas het
   component eraan hangen. 8 + 30 rondes. Alle rondes OK.
2. **Gelijktijdig** (de scherpste): laat twee componentpatches NOG lopen terwijl de
   deployment wordt aangemaakt, en controleer daarna ook in git of de deployment er
   werkelijk staat. 8 + 30 rondes. Alle rondes OK, deployment steeds in het
   projectbestand.
3. **Gelijktijdig MET uitrol** (het grootste venster): dezelfde vorm, maar de
   componentpatches met `rollout=true`, zodat ze het hele project verwerken en minuten
   duren in plaats van seconden. Ze lopen dan gegarandeerd nog terwijl de deployment
   geschreven wordt. Alle rondes OK.
4. **Zonder wachten**: het paar afvuren zonder de eerste taak af te wachten. Ook dit
   gaat goed -- en dat is zelf een bevinding, zie hieronder.

Meetregels per ronde staan in de test zelf (`PUNT14_METINGEN`); een ronde kost 4 tot 8
seconden voor beide taken samen.

### Wat we wel hebben aangewezen, en wat het waarschijnlijk niet is

Het plan verklaarde de cacheverklaring al dood: `ProjectStore.read_path` bedient een
HEAD-lezing uit de in-memory cache, en `save` heeft een `refresh_cache`-schakelaar die
geen enkele aanroeper uitzet. Dat klopt en is hier niet verder achterna gelopen.

Wat er wel gevonden is, en wat de beste kandidaat blijft:

**De taakpoort sluit twee schrijvers op hetzelfde projectbestand niet altijd uit.**
`AsyncTaskService.claim_next_task` slaat een wachtende taak alleen over als er al een
taak loopt met hetzelfde project **en dezelfde deploymentnaam**
(`running.deployment_name.is_not_distinct_from(AsyncTask.deployment_name)`). En:

* `update_component` (PATCH op een component) maakt zijn taak **zonder** deploymentnaam;
* `upsert_deployment` en `add_component_to_deployment` maken hem **met**.

`NULL` is niet gelijk aan `productie`, dus die taken draaien wel degelijk gelijktijdig
over hetzelfde projectbestand. `find_conflicting_task` waarschuwt bovendien alleen bij
hetzelfde `task_type`, dus dit paar geeft ook geen enkele regel in de logs.

Dat de deployment daarbij toch niet verdwijnt, komt door de laag eronder: de store
serialiseert schrijvers met een lock en doet een compare-and-swap met een drieweg-merge.
In ronde 1 stonden er 15 drieweg-merges in de logs; veertien daarvan losten netjes op.
Zolang die merge blijft werken, is dit gat afgedekt -- maar het is wel het gat waardoor
een verloren wijziging zou binnenkomen, en het is de plek om te kijken als punt 14 weer
opduikt.

Dat vorm 3 (zonder wachten) ook goed gaat, past bij die lezing: die twee taken hebben
**wel** dezelfde deploymentnaam, dus daar grijpt de poort juist wel, en de tweede taak
wacht netjes op de eerste.

### Wat de volgende zou moeten doen

Wat wij niet hebben nagebootst is het draaiboek zelf: 44 stappen achter elkaar, met alle
tussenliggende commando's (diensten aanzetten, images bijwerken, `describe`) ertussen.
Wij hebben het paar uit stap 11 geisoleerd en herhaald. Als punt 14 aan een specifieke
VOLGORDE van eerdere stappen hangt, en niet aan de gelijktijdigheid, dan zit het daar en
niet in wat wij gemeten hebben. Het draaiboek van de zad-cli zelf laten draaien is dan de
volgende stap.

## Wat de suite over gelijktijdigheid zegt

Dit was de eerste reallife-run sinds de ProjectStore en het dienstensysteem er zijn.
Per punt uit het plan:

### Conflicten bij het opslaan: die kwamen als 500 naar buiten (gerepareerd)

**Dit is de belangrijkste bevinding naast punt 14, en het zat in de CODE, niet in de test.**

In ronde 1 raakten een API-patch en een verwijdering in de componenten-modal hetzelfde
projectbestand. De compare-and-swap zag een botsing op hetzelfde onderdeel en gooide
`ConflictError` -- met een keurige uitleg erin, geschreven voor de gebruiker. Alleen ving
niemand hem:

```
POST /projects/rl089-v3y/modal-wizard/modal-edit-components/step/components-edit  500 Internal Server Error
  opi/web/router_detail_edit.py, in _process_and_save_modal_edit
  opi/services/project_store.py, in _reconcile_with_concurrent_write
    raise ConflictError(...)
```

Dezelfde functie had die weg al wel voor validatiefouten: die worden opgevangen en als
melding op de review getoond. `ConflictError` stond alleen niet in de rij. Dat is
gerepareerd (`tests/test_modal_edit_conflict.py`, die faalt als je de reparatie
terugdraait), en de tekst van de melding is meteen van "u" naar "je" gezet omdat hij nu
werkelijk op het scherm komt.

In ronde 2, met de reparatie erin: **0 keer 500, 0 onbehandelde ConflictError.**

### Onder dubbele belasting valt de hele API even weg (503)

Ronde 3 draaide de reallife-suite en de uitrol-variant van de jacht **tegelijk**: samen
zo'n tien gelijktijdige ArgoCD-wachten plus schrijvers op het projectbestand. Halverwege
brak de jacht af op iets wat niet uit de applicatie kwam:

```
POST /api/v2/projects/p1492-4f2/:upsert-deployment?rollout=false -> 503
<center><h1>503 Service Temporarily Unavailable</h1></center>
<hr><center>nginx</center>
```

Dat is de ingress, niet OPI. De pod was niet omgevallen (`Restart Count: 0`), maar:

```
Warning  Unhealthy  Readiness probe failed: Get "http://10.244.0.53:8000/readyz":
                    context deadline exceeded (Client.Timeout exceeded while awaiting headers)
```

Twee keer, op de twee drukste momenten. `/readyz` doet zelf niets zwaars -- het leest een
vlag uit het geheugen -- dus "deadline exceeded" betekent dat de handler niet aan de beurt
kwam: de eventloop stond langer dan de 5 seconden probetimeout vol. In dezelfde periode
duurden store-persists tot 9,2 seconden.

Wat het erg maakt is niet de trage seconde maar de instelling eronder:
`readinessProbe.failureThreshold: 1`. Eén trage probe haalt de pod meteen uit de
endpoints van de service, en dan krijgt **elke** lopende API-aanroep een 503 van nginx --
ook aanroepen die niets met de drukte te maken hebben. Voor een client als de zad-cli is
dat niet te onderscheiden van een echte fout.

Niet gerepareerd in deze PR: zowel de probe-instelling als het opsporen van wat de
eventloop vasthoudt is een eigen taak met een eigen meting. Wel vastgelegd, want dit is
een echte beschikbaarheidsfout en hij is nu voor het eerst zichtbaar gemaakt.

Om te laten zien dat het aan de belasting lag en niet aan de variant, is diezelfde
uitrol-variant daarna nog een keer alleen gedraaid.

### Taken die op elkaars projectbestand wachten

Gemeten, en het werkt zoals bedoeld: de storelock serialiseert, en 73 taken werden in
ronde 1 als `superseded` afgesloten -- een nieuwere, bredere taak nam hun ArgoCD-wacht
over. Dat is de bedoelde uitkomst en de suite telt hem ook als goed. Geen enkele taak
liep vast in de wacht.

### Een tweede taak die een half doorgevoerde wijziging ziet

Niet waargenomen. De cache wordt pas na een geslaagde push bijgewerkt, en de regel die
de store zelf logt als de cache van git zou zijn afgedreven ("ProjectStore cache had
drifted from git") kwam in geen van beide rondes voor.

## Bevindingen per stuk: test of code

| # | Bevinding | Test of code |
|---|---|---|
| 1 | `ConflictError` in de bewerkmodal kwam als 500 naar buiten | **Code** -- gerepareerd in deze PR |
| 2 | De taakpoort laat `update_component` en `upsert_deployment` op één project gelijktijdig lopen | **Code** -- niet gerepareerd, zie hierboven; afgedekt door de drieweg-merge |
| 3 | Meetregels van de punt-14-jacht kwamen niet in de uitvoer terecht | **Test** -- opgelost met `PUNT14_METINGEN` |
| 4 | Readinessprobe loopt in zijn timeout onder dubbele belasting -> de hele API geeft 503 | **Code/infra** -- niet gerepareerd, zie hierboven |
| 5 | `test_ui_removal_while_api_patches_same_file` blijft xfail | **Code, bekend** -- een component uit de modal halen laat zijn verwijzing in de deployment staan; staat al als strict xfail met uitleg |

## Wat er niet in zit

Repareren van punt 14. Het is niet gereproduceerd, dus er is niets aan te wijzen om te
repareren; de verruiming van de taakpoort is een eigen afweging met een eigen test.
