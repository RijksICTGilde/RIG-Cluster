# Alles wat sinds 2 september nieuw is, op het cluster toetsen

Status: plan, 25 september 2026.

Productie draait `2026.09.02.2241-d81cdab4`. Dat is de pin in `bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production/patches/deployment.yaml:12`, en die pin is de deploybeslissing. Tussen die commit en de huidige main zitten **449 commits**, waarvan 27 met een `feat`-voorvoegsel en 97 met `fix`. De releasenotitie van 22 september beschrijft een deel daarvan als opgeleverd, maar geen regel ervan draait op productie.

Dit plan zet die 449 commits om in een lijst van toetsbare functies, en toetst ze op een draaiend cluster in plaats van alleen in de unittests. De vraag per functie is steeds dezelfde: doet hij het daar, en staat er een toets die het bewaakt als niemand kijkt.

## Wat dit plan aanneemt

**Met "het server cluster" wordt het gedeelde sandboxcluster bedoeld**, het Kind-cluster achter Caddy op de dev-server (`docs/sandbox-on-dev-server.md`), bereikbaar op `https://zad.sandbox.rijksapp.dev`. Dat is de enige plek waar een echte uitrol te doen is zonder productie te raken.

**Dit plan wordt uitgevoerd door een sessie op diezelfde dev-server, en die zet de sandbox zelf aan.** Dat is geen voorwaarde vooraf en geen handmatige stap: `sandbox-deploy` staat daar in de PATH en doet de hele weg in een keer, inclusief het claimen van de lock. Loopt dat vast, dan is dat een vondst die bij dit plan hoort en geen reden om te stoppen.

Let op bij het meten van buitenaf: `*.sandbox.rijksapp.dev` resolveert naar `127.0.0.1`, dus een `curl` vanaf een andere machine raakt die machine zelf en zegt niets over de sandbox op de server. Meet altijd vanaf de server.

## Wat er al staat, en wat dat waard is

Er is een werkende vorm voor precies dit soort toetsen, en die hoeft niet uitgevonden te worden:

- **26 sandbox-toetsen** in `operations-manager/python/tests/e2e/test_sandbox_*.py`, gemarkeerd met `@pytest.mark.sandbox`, gedreven door Playwright (de UI) plus de `sandbox_api`-helper.
- **`task test-e2e-sandbox`** draait ze: `pytest tests/e2e/ -m "e2e and sandbox and not reallife"`.
- **`task test-e2e-sandbox-reallife`** draait de lange variant met vijf projecten en gelijktijdige mutaties.
- **99 e2e-bestanden in totaal**, waarvan het merendeel tegen een losse testserver draait en geen cluster nodig heeft.

De gaten zitten niet in de vorm maar in de dekking, en op twee plekken:

1. **Nieuwe functies zonder sandboxtoets.** De unittests zijn er wel (12043 groen), maar niets meet of het in een echt cluster werkt.
2. **zadctl komt in de toetsen bijna niet voor.** Eén bestand noemt hem (`tests/e2e/test_introductiepagina_beeld.py`). De CLI is 0.10.0 en heeft subcommando's voor vrijwel alles wat hier nieuw is (`registry`, `service`, `resource`, `clone`, `restore`, `env`, `alias`, `attachment`), maar geen enkele toets loopt dat pad af. Een API-wijziging die de UI niet raakt maar de CLI wel, valt vandaag nergens om.

## De lijst: wat er nieuw is

Zeven blokken, gesorteerd op hoeveel er aan gewerkt is. Per functie staat waar hij te bedienen is, wat er al aan toetsen ligt en wat er ontbreekt.

### 1. Eigen container registries (26 + 9 commits, `features/image-registries.md`)

Verreweg het grootste blok. Een project vult registry, gebruikersnaam en token in; per component welke registry bij welke image hoort.

| Functie | UI | zadctl | Toets nu |
|---|---|---|---|
| De dienst, de regelvorm en de twee bronnen | ja | `registry list/add` | unittests |
| Lijst op dienstniveau, projectniveau ontstaat | ja | `registry` | unittests |
| De tokentoets voor opslaan | ja | `registry add` | unittests |
| De eigendomscontrole en het serviceaccount per project | nee | nee | `test_sandbox_registry_ownership.py` |
| Het registryblok op de projectpagina | ja | nee | e2e zonder cluster |
| De registrykeuze staat bij de image, en die keuze is de selectie | ja | `component update` | `test_image_registries_component_choice.py` |
| De upstream volgt uit de images die het project al heeft | ja | nee | unittests |
| Geplakte URL en vrij label rechttrekken | ja | nee | unittests |
| `WidgetType.PASSWORD` op het token | ja | nvt | unittests |
| Standaardoptie heet "Automatisch" | ja | nee | geen |

**Ontbreekt:** een sandboxronde die een echte private registry aanmaakt, een image eruit draait en aantoont dat het pull-secret in de goede namespace landt. `test_sandbox_registry_ownership.py` dekt de tenantscheiding, niet de gewone weg.

### 2. De sandbox zelf (14 commits + 2 kind-registry + 3 skopeo)

De bouwweg van de sandbox is vervangen: `kind load` eruit, een registry naast het cluster erin.

- De build pusht het image naar de registry naast het cluster
- Registry naast het cluster staat deletes toe, bestaande container behoudt zijn volume
- Oude tags verlopen na twee dagen, de gedeelde lagen blijven
- `features/image-lagen.md`: wat er per deploy opnieuw door de leiding gaat

**Dit is geen functie voor een gebruiker maar het fundament onder elke andere toets in dit plan.** Werkt dit niet, dan komt er niets op het cluster. Het toetst zichzelf door gebruikt te worden, en dat is hier genoeg: fase 0 is die toets.

**Ontbreekt:** niets aparts, behalve dat de documentatie eromheen nog half de oude weg beschrijft. Zie de waarschuwing bij fase 0.

### 3. Diensten: speelruimte en selectie (5 + 5 commits)

`features/speelruimte-van-een-dienst.md`: een dienst declareert per veld de ondergrens, de bovengrens, de standaardwaarde en op welke lagen het veld gezet mag worden. Uit die ene declaratie lezen drie plekken.

| Functie | UI | zadctl | Toets nu |
|---|---|---|---|
| Speelruimte per veld, drie lezers uit één declaratie | ja | `service config set` | unittests |
| De speelruimte-guard sluit ook als het veld ontbreekt | ja | `service config set` | unittests |
| Een `grow_only`-veld staat op precies één laag | ja | `service config set` | unittests |
| Een kale dienst is de standaard, ook als VORIGE versie | ja | `service` | unittests |
| Een plek is de mount, niet alleen de component | ja | `attachment` | unittests |
| Dienst zegt zelf of je hem per component aanvinkt | ja | `component update --service` | unittests |
| De connectielimiet van de databaserollen is instelbaar | ja | `service config set` | `test_connection_limit.py` |

**Ontbreekt:** geen enkele sandboxtoets raakt de speelruimte. De guard is juist het soort ding dat in een unittest klopt en in het echt langs de rand glipt, want daar komt de waarde uit een formulier of uit de CLI en niet uit een testfixture. Dit is het blok waar een zadctl-toets het meest oplevert: `service config set` met een waarde onder de ondergrens, erboven, en op de verkeerde laag.

### 4. Klonen en herstellen (5 + 1 commits, `features/kloonpoging.md`)

- Een afgebroken kloon wordt afgemaakt in plaats van opnieuw begonnen
- De pogingsvlag gaat pas aan als de databasekloon echt begint
- Geen pogingsvlag boven een doelschema dat er al stond
- Het bronschema van een remote source wordt niet meer gedropt
- Een losse PVC-restore geeft zijn cluster door aan de pod

**Toets nu:** `test_clone_attempt_flag.py` (unit), en aan de sandboxkant `test_sandbox_restore_*.py` (vijf bestanden) voor de restorekant.

**Ontbreekt:** de klonkant heeft nul sandboxtoetsen. En dit is precies een functie over een afgebroken run, dus de enige eerlijke toets breekt de run halverwege af en start hem opnieuw. Dat kan alleen op een cluster.

### 5. Foutmeldingen (4 `web` + 4 `validatie` commits, `features/foutmeldingen.md`)

- Een 5xx komt aan als pagina of als envelop, niet als kale JSON
- De foutenvelop staat in het OpenAPI-document
- De webpagina's melden wat je eraan kunt doen
- Technische details lekken niet meer: gereedschapsroutes, geweigerde instelling, registrytoets
- Een onbekende statuscode laat de foutafhandeling niet omvallen

**Toets nu:** `test_server_error_page.py` (unit).

**Ontbreekt:** alles op clusterniveau. Dit is bij uitstek een zadctl-toets: de CLI is de tweede afnemer van diezelfde envelop, en of hij daar een leesbare regel van maakt of een stapel JSON uitbraakt, is nu nergens vastgelegd. Meet ook dat er geen traceback in de tekst staat, want dat is de hele reden dat dit blok bestaat.

### 6. Reconciliatie (4 commits)

- Tabel `project_reconciliation` met migratie 006
- `process_project` legt vast wat hij reconcilieerde
- Backfill in migratie 006 uit de oude meting
- asyncpg-verbindingsfouten vangen, restore telt als uitrol

**Toets nu:** 17 bestanden raken het, waaronder `test_migration_006_project_reconciliation.py`.

**Ontbreekt:** de migratie is op een lege testdatabase getoetst, niet op een database met historie erin. De backfill is het risico: die leest de oude meting, en of dat op echte rijen klopt weet je pas als je het op een sandbox met een gevulde database draait. `test_sandbox_reallife.py` raakt reconciliatie zijdelings.

### 7. Losse functies

| Functie | Waar | Toets nu |
|---|---|---|
| Gereserveerde subdomeinen alleen op zones die ZAD zelf bedient | UI + projectbestand | 7 bestanden |
| Uitnodiging: bevestigingsmail meteen, pagina zegt het | UI | `test_sandbox_invite_ui.py` |
| Uitnodiging: component-keuze opgeslagen, niet de URL | UI | unittests |
| Uitnodiging: afgekeurde bestemming niet terug in de fout | UI | unittests |
| Keycloak: elke gebruiker met een adres bevestigt bij aanmaak | UI | unittests |
| VLAM: doorlus naar overheid-i met de bundel erbij | UI | `test_sandbox_vlam.py` |
| VLAM: CA-bundel te downloaden bij het dienstblok | UI | geen |
| VLAM: doorlus-pad als geheel uit de clusterconfiguratie | config | unittests |
| Het repositorypad blijft binnen de repo | projectbestand | `test_repository_pad_binnen_de_repo.py` |
| De ciphertext van een secret dat deze run opnieuw gemaakt wordt blijft staan | onzichtbaar | unittests + `test_sandbox_secret_rollout.py` |

## De aanpak

Vier fases. Elke fase levert iets af dat blijft staan, en geen enkele fase hangt van de volgende af.

### Fase 0: de sandbox aan de praat krijgen

Dit is werk van de uitvoerende sessie zelf, niet iets dat klaar moet staan.

1. `sandbox-deploy` draaien. Dat claimt de lock, bouwt de operations-manager uit deze checkout, zet hem op het cluster en toetst `/version`. `task sandbox:update-operations-manager` en `task sandbox:setup` zijn hier uitdrukkelijk NIET de weg: die zijn voor een volledige lokale opzet en missen hier wat ze nodig hebben.
2. Toetsen dat het de goede versie is: `curl -sk https://zad.sandbox.rijksapp.dev/version` moet dezelfde commit noemen als `git rev-parse --short HEAD`. Doet hij dat niet, dan is `opi/version.json` niet opnieuw gegenereerd en draait er iets anders dan je denkt.
3. `zadctl version` moet zowel de CLI-versie als de serverversie tonen. Dat is meteen de eerste toets dat de CLI het cluster kan bereiken, en fase 1 bouwt daarop verder.
4. De bestaande 26 sandboxtoetsen draaien: `task test-e2e-sandbox`. Dit is de nulmeting. Wat hier al rood staat is een vondst van vandaag en niet iets dat de nieuwe toetsen straks moeten opvangen.
5. `sandbox-release` aan het eind van de hele ronde, niet tussendoor. De lock houden is juist de bedoeling zolang je itereert: de sandbox is een gedeelde, schaarse bron en precies een PR tegelijk mag hem hebben.

**Twee dingen die hier kunnen wringen.** `workflow/sandbox.md` spreekt zichzelf tegen over de bouwweg: regel 84 zegt dat de deploy naar de registry naast het cluster pusht, regels 7, 107 en 165 zeggen nog `kind load`. Dat is het blok van 14 sandbox-commits dat halverwege in de documentatie is geland. Klopt het commando niet meer met de tekst, repareer dan de tekst, want die staat op main en leidt de volgende sessie ook verkeerd.

En als `sandbox-deploy` op dat punt omvalt, is dat geen blokkade van dit plan maar de eerste echte vondst ervan: blok 2 uit de lijst hieronder gaat precies daarover, en het toetst zichzelf door gebruikt te worden.

Verifieer: `/version` op het cluster noemt de commit van deze branch, `zadctl version` bereikt de server, en de uitslag van de nulmeting staat opgeschreven.

### Fase 1: de zadctl-weg openen

Vandaag loopt geen enkele toets over de CLI. Dat is het grootste gat, want de CLI is een tweede afnemer van dezelfde API met eigen aannames over de antwoordvorm.

4. Een helper bouwen die `zadctl` aanroept tegen de sandbox met een projectsleutel, en die stdout, stderr en de exitcode teruggeeft. Voorstel voor de plek: `tests/e2e/helpers/`, naast de bestaande `sandbox_api`. De naam is een voorstel, niet iets dat al bestaat.
5. Eén doorloop als eerste klant: project aanmaken, component toevoegen, uitrollen, status opvragen, opruimen. Dit is de rookmelder voor alles daarna.

Verifieer: de doorloop draait groen onder `-m "e2e and sandbox"`, en valt om als je de API-URL naar een dood adres wijst.

### Fase 2: de vier blokken zonder clusterdekking

Op volgorde van wat het meest kan stukgaan zonder dat iemand het merkt.

6. **Speelruimte van een dienst** via `zadctl service config set`: een waarde onder de ondergrens, een erboven, een op de verkeerde laag, en een `grow_only`-veld dat omlaag wil. Alle vier moeten weigeren, en de weigering moet zeggen wat mag.
7. **Foutmeldingen**, via dezelfde weg: een 5xx uitlokken en meten dat de CLI er een leesbare regel van maakt, dat er geen traceback in staat, en dat de envelop de vorm heeft die in het OpenAPI-document beschreven staat.
8. **Een afgebroken kloon**: een deployment met `clone-from`, de run halverwege afbreken, opnieuw draaien, en meten dat de kloon wordt afgemaakt in plaats van dat er een `_vN`-database naast komt. Dit is de enige toets in dit plan die een storing moet nabootsen; zonder dat meet hij niets.
9. **Registries, de gewone weg**: een echte private registry, een image eruit, en aantonen dat het pull-secret in de goede namespace staat en de pod start.

Verifieer per stap: de toets valt om op de code van vóór de betreffende commit. Een toets die op allebei groen staat, meet iets anders dan hij belooft.

### Fase 3: de migratie op echte data

10. De sandbox met historie vullen (of `test_sandbox_reallife.py` eerst draaien, die maakt vijf projecten aan), en dan pas migratie 006 draaien. Meten dat de backfill rijen oplevert die kloppen met de oude meting, en niet alleen dat de migratie zonder fout eindigt.

Verifieer: de tabel `project_reconciliation` heeft na de backfill rijen voor projecten die al bestonden, en de waarden komen overeen met de oude meting.

### Fase 4: de rest langslopen

11. Wat in de tabellen hierboven wel een toets heeft maar geen sandboxtoets, en niet in fase 2 zat, één keer met de hand door de UI lopen op de sandbox, en per functie in één regel vastleggen wat er gemeten is. Dit levert geen toetsen op, wel een uitspraak.
12. Wat daarbij omvalt, wordt een issue, geen reparatie in deze ronde. Anders loopt dit plan leeg in het repareren van iets anders.

## Wat erbuiten valt

**Productie aanzetten.** Dit plan meet of main klaar is om uitgerold te worden. De uitrol zelf, en het bumpen van de CalVer-pin in de odcn-overlay, is een aparte beslissing die hierna komt.

**De 168 docs-commits.** Die veranderen geen gedrag.

**De securityronde.** De sleutelrotatie is op 24 september op productie uitgevoerd en daar afgetoetst met een eigen final check. Het rotatiegereedschap heeft eigen toetsen (142 groen), en `--own-values-on-another-key` is op 25 september alsnog rechtgetrokken. Dat hoort niet in deze ronde thuis.

**Repareren wat omvalt.** Behalve als het de toets zelf is die fout staat.

## Hoe je weet dat het werkt

Aan het eind zijn er drie dingen die er nu niet zijn:

1. **Een zadctl-pad in de toetsen**, dat over dezelfde API loopt als de UI maar er andere aannames over heeft, en dat omvalt als de antwoordvorm verandert.
2. **Vier nieuwe sandboxtoetsen** voor de blokken die vandaag alleen unittests hebben, en elk daarvan valt aantoonbaar om op de code van ervoor.
3. **Een uitspraak per functie uit de lijst**: gemeten en goed, gemeten en stuk (met issuenummer), of niet gemeten en waarom.

Die derde is de belangrijkste, want de eerste twee zeggen alleen iets over wat we gebouwd hebben, en de derde zegt iets over wat er draait.
