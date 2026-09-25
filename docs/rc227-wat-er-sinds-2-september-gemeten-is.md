# Wat er sinds 2 september op het cluster gemeten is (RC-227)

Productie draait `2026.09.02.2241-d81cdab4`. Tussen die commit en main zitten 449 commits.
Dit document zegt per functie uit dat blok wat er op een DRAAIEND cluster van gemeten is:
**gemeten en goed**, **gemeten en stuk**, of **niet gemeten, en waarom**. Niet wat de
unittests zeggen, want die stonden al groen.

Gemeten op het gedeelde sandboxcluster (`https://zad.sandbox.rijksapp.dev`) met build
`65b90336` op branch `alles-wat-sinds-2-september-nieuw-is-op-het-cluste`, op 24 september
2026.

> Dit is een momentopname en geen lopend document. Wat blijvend bewaakt moet worden staat
> als toets in `operations-manager/python/tests/e2e/test_sandbox_*.py`; wat hier staat en
> daar niet, is met opzet een eenmalige uitspraak.

## Wat de uitrol zelf kostte

Drie dingen stonden tussen main en een draaiend cluster in. Ze horen in dit document
omdat ze precies de vraag beantwoorden die dit plan stelde.

| Wat | Uitkomst |
|---|---|
| Het cluster miste `containerdConfigPatches` | **gemeten en stuk, opgelost buiten git.** `sandbox-deploy` stopte met exit 4. Een kind-config geldt alleen bij `kind create cluster`, dus een cluster van voor 2026-09-16 mist de regel. Bijgeschreven volgens `docs/sandbox-kind-registry.md`, sectie "Een bestaand cluster bijwerken zonder herbouw". De nieuwe bouwweg werkt, maar niet op een cluster dat er al stond. |
| `quay.io/minio/mc` geeft 401 | **gemeten en stuk, gerepareerd** (commit `65b90336`). Controlebeeld: `quay.io/prometheus/busybox` geeft 200 vanaf dezelfde machine, `quay.io/minio/mc` en `quay.io/minio/minio` allebei 401. mc komt nu uit de GitHub-release van dezelfde tag, met de sha256sum ernaast als grendel. |
| De productiepin is niet meer te herbouwen | **gemeten en stuk, niet gerepareerd.** De Dockerfile op `d81cdab4` haalt mc van `dl.min.io`, en dat adres geeft 410. Een terugrol naar de huidige productiepin kan alleen met het al gepubliceerde image, niet met een build. |

## De nulmeting, en waarom er geen volledige staat

Fase 0 van het plan vroeg om de bestaande sandboxtoetsen als nulmeting. Die run is gestart
en na 25 van de 74 toetsen afgebroken. Wat er tot dat punt stond: 22 groen, en drie ERRORs
in de fixture van `test_sandbox_all_services.py`.

Die drie zijn later thuisgebracht, en het is geen codefout maar ook niet zomaar ruis. Elke
fixture-ERROR in elke gang van deze ronde komt van dezelfde plek: de aanmaaktaak van OPI
eindigt met

```
Failed: <project>-productie (productie): timed out after 300s waiting for sync
```

waarna de wizardhelper de link naar de detailpagina nooit ziet en op zijn eigen wacht
strandt. De applicatie komt er daarna gewoon (nagemeten: `speel-2il-productie` stond op
`Synced` en de pod draaide, kort nadat de taak al als mislukt was weggeschreven).

**Het is niet systematisch, het hangt aan de drukte van het cluster.** In dezelfde ronde
haalden projecten met dezelfde diensten het wel binnen de tijd, ook met `postgresql-database`
erbij. Wat het betekent voor wie hier toetsen draait:

- **een sandboxsuite is niet betrouwbaar naast een andere suite**, en ook niet direct achter
  een reeks aanmaakrondes aan. Laat het cluster eerst leeglopen;
- **de 300s die OPI aanhoudt voor de ArgoCD-sync is op dit cluster krap.** Een taak die daar
  overheen gaat wordt als mislukt vastgelegd terwijl het project een halve minuut later
  draait. Dat is een uitspraak over deze sandbox, niet over productie, maar het maakt elke
  aanmaaktoets hier wankel.

**Een nulmeting hoort dus nog te gebeuren, op een leeg en rustig cluster en als enige run.**
Zonder die meting is niet te zeggen of iets wat later rood staat van de nieuwe code komt of
er al stond.

## 1. Eigen container registries

| Functie | Uitspraak |
|---|---|
| De dienst, de regelvorm en de twee bronnen | **gemeten en goed.** `zad registry add` schrijft de entry in het projectbestand, met het token versleuteld (`test_sandbox_registry_pull.py`). |
| De tokentoets voor opslaan | **gemeten en goed** via dezelfde weg. |
| Het registryblok op de projectpagina | **gemeten en goed.** De kaart "Eigen container registries" staat op het tabblad Services met een werkende "Configureer"-knop; het scherm is bekeken en niet alleen gegrept. |
| `WidgetType.PASSWORD` op het token | **gemeten en goed.** Het tokenveld staat in de modal afgeschermd (`type="password"`, op het scherm een rij bolletjes). |
| Geplakte URL en vrij label rechttrekken | **niet gemeten.** Vergt invullen en opslaan in het formulier; de unittests dekken de converter en de clustertoets zou dezelfde converter meten. |
| De upstream volgt uit de images die het project al heeft | **niet gemeten.** Zelfde reden: een `default` die alleen bij een NIEUWE lege rij geldt, en die weg loopt niet over de API. |
| De eigendomscontrole en het serviceaccount per project | **al gedekt** door `test_sandbox_registry_ownership.py`, ongewijzigd. |
| De registrykeuze staat bij de image, en het pull-secret landt | **gemeten**, zie `test_sandbox_registry_pull.py`. Uitkomst staat in de PR. Let op de ORDE waarin het landt, zie "Wat hieruit volgt". |
| Standaardoptie heet "Automatisch" | **niet gemeten.** Komt op de componentkant van de dienst; de modal op projectniveau toont hem niet. |

**Waarneming, geen bevinding.** Het tokenveld heet op het scherm "Token *Optioneel*", terwijl
`features/image-registries.md` het kopje "De gebruikersnaam is optioneel, het token niet"
draagt. Beide kloppen: het veld is niet `required` in `editables.py` omdat een entry ook een
`secretName` kan dragen, en `_has_exactly_one_way_to_pull` weigert bij het opslaan als geen
van beide er is. Maar dat `secretName`-veld staat niet in dit formulier, dus voor wie dit
scherm gebruikt is het woord "Optioneel" onwaar: leeg laten levert een weigering op. Dit is
een label-kwestie, geen defect in de code, en het is met opzet niet in deze ronde
gerepareerd.

## 2. De sandbox zelf

**Gemeten door gebruikt te worden**, zoals het plan voorzag. De build pusht naar de registry
naast het cluster en de node pullt eruit; dat is de enige reden dat er iets te meten viel.
Wat er niet vanzelf uit volgde staat in "Wat de uitrol zelf kostte" hierboven.

## 3. Diensten: speelruimte en selectie

| Functie | Uitspraak |
|---|---|
| Speelruimte per veld, drie lezers uit een declaratie | **gemeten** over de API (`test_sandbox_speelruimte.py`): binnen de grenzen wordt opgeslagen, onder de ondergrens en boven de bovengrens geweigerd, de weigering noemt de grenzen, en het projectbestand blijft onveranderd. Ook over de CLI gemeten, als paar: een te lage waarde raakt het projectbestand niet en een geldige wel. Zie "Wat de CLI-weg opleverde" voor waarom die helft er eerst niet stond. |
| De guard sluit ook als het veld ontbreekt | **gemeten** als de servertoets op een niet-gedeclareerd veld. |
| De weigering noemt de speelruimte | **gemeten en goed**, letterlijk: `Project 'speel-lco': configuratie van service 'postgresql-database' op projectniveau is ongeldig: 'connection-limit' moet tussen 1 en 500 liggen; je gaf 501.` |
| Een laag die de dienst niet openzet | **gemeten en goed.** `connection-limit` staat op project en deployment; de componentlaag bestaat als route niet eens (`/services/postgresql-database/config/component/...` staat niet in het OpenAPI-document van de draaiende server). |
| Een `grow_only`-veld staat op precies een laag | **niet gemeten, en dat kan ook niet.** `grow_only` bestaat in het mechanisme, maar geen enkele dienst declareert een veld met die vlag: buiten `config_settings.py` komt het woord in `opi/` niet voor. Er is niets om op te richten. Zodra een dienst er een declareert, hoort er een clustertoets bij. |
| De connectielimiet is instelbaar | **gemeten**, dit is het veld waarop het bovenstaande gemeten is. |
| Een kale dienst is de standaard, ook als VORIGE versie | **niet gemeten.** Een uitspraak over twee versies van hetzelfde bestand; op een cluster alleen te maken door twee keer op te slaan, en dan meet je de opslagvolgorde. |

**Waarneming die in de feature-doc ontbreekt.** De grendel zit in de VERWERKING en niet op
de HTTP-grens. `PUT .../services/postgresql-database/config/project` antwoordt **202** met
een taak-id, ook op een waarde buiten de speelruimte; de weigering komt pas terug in de
uitkomst van die taak. Voor een gebruiker ziet een ongeldige waarde er dus eerst aangenomen
uit. Het projectbestand blijft wel onveranderd, dus er gaat niets mis, maar wie op de
HTTP-status afgaat leest het verkeerd. Een toets die op een 4xx wacht meet hier niets, en
daar is deze suite ook eerst op omgevallen.

## 4. Klonen en herstellen

| Functie | Uitspraak |
|---|---|
| Een kloon neemt de data mee | **gemeten** met een merkteken in de brondatabase (`test_sandbox_kloon.py`). |
| Geen `_vN` naast de doeldatabase, ook niet na een tweede run | **gemeten**, en dit is de kern van het blok. |
| De pogingsvlag gaat uit bij het vastleggen | **gemeten** in het projectbestand. |
| Een afgebroken run wordt afgemaakt | **niet gemeten.** De afgebroken staat is "vlag op schijf, doeldatabase aanwezig, doelschema's afwezig", en die is op een GEDEELD cluster alleen te maken door OPI midden in een run om te leggen (raakt de run van een ander) of door het projectbestand met de hand te schrijven (dan meet je de toets). Blijft bij `tests/test_clone_attempt_flag.py`. |
| De restorekant | **al gedekt** door vijf `test_sandbox_restore_*.py`-bestanden. |

## 5. Foutmeldingen

| Functie | Uitspraak |
|---|---|
| Een 5xx komt aan als pagina of als envelop | **gemeten en goed** voor de keuze zelf: onder `/api` komt nooit markup uit, ook niet met een browser-`Accept`; buiten `/api` krijgt een ingelogde browser een pagina en een JSON-client JSON. |
| De foutenvelop staat in het OpenAPI-document | **gemeten en goed** op het document dat de draaiende server publiceert: `ProblemDetail` met alle zeven velden, en elke `/api`-operatie noemt `5XX` en wijst ernaar. |
| Technische details lekken niet | **gemeten en goed** op drie paden: geen traceback, geen `/app/`-pad, geen driver- of poortnaam. |
| Wat de CLI van de envelop maakt | **gemeten en goed** tegen een stub die de gedocumenteerde envelop teruggeeft: de zin uit `detail` komt bij de gebruiker, het kenmerk staat erbij, en er komt geen ruwe JSON of traceback uit. |
| Een onbekende statuscode | **niet gemeten.** Vergt een antwoord dat de server niet uit zichzelf geeft. |

**Waarneming.** Een onbekend pad BUITEN `/api` geeft een ANONIEME browser 302 naar de login,
geen 404. Dat is juist (de inlogpoort zit voor de routering), maar het betekent dat de
404-pagina alleen ingelogd te meten is. Twee toetsen hier stonden daardoor eerst rood.

## 6. Reconciliatie

| Functie | Uitspraak |
|---|---|
| Tabel `project_reconciliation` met migratie 006 | **gemeten en goed.** Tabel, unieke index en `alembic_version = 006` staan op het cluster. |
| De backfill op echte rijen | **gemeten en goed**, met een uitkomst die uitleg nodig heeft: op 1257 echte `async_tasks`-rijen levert hij NUL rijen op, want `refresh_project` en `delete_component` komen in die historie niet voor. Dat is het juiste antwoord: de backfill reproduceert de oude meting (`ROLLOUT_CLEARING_TASK_TYPES`, verwijderd in `9ec42982`), en die zag daar ook niets. De toets meet daarom een gelijkheid en geen aantal. |
| Het statement is idempotent | **gemeten en goed.** Twee keer draaien voegt niets toe, en dat toetst de `ON CONFLICT` op `COALESCE(deployment_name, '')`: zonder die COALESCE laat Postgres twee rijen met `deployment_name IS NULL` naast elkaar staan. |
| `process_project` legt vast wat hij reconcilieerde | **gemeten, zijdelings.** De tabel draagt 22 rijen die niet uit de backfill komen, dus er wordt geschreven. |

## 7. Losse functies

| Functie | Uitspraak |
|---|---|
| Gereserveerde subdomeinen alleen op zones die ZAD bedient | **gemeten en goed.** Op `sandbox.rijksapp.dev` worden `admin` en `www` geweigerd (`cluster_domain: true`); dezelfde namen op `eigendomein.example.nl` zijn beschikbaar (`cluster_domain: false`); een gewone naam mag op allebei. |
| VLAM: CA-bundel te downloaden bij het dienstblok | **gemeten en goed.** `GET /services/vlam/ca-bundle` geeft 200, `application/x-pem-file`, 5603 bytes, met `Content-Disposition: attachment; filename="vlam-ca.pem"`. Dit had geen enkele toets. |
| VLAM: doorlus naar overheid-i met de bundel | **al gedekt** door `test_sandbox_vlam.py`. |
| Uitnodiging: bevestigingsmail en pagina | **al gedekt** door `test_sandbox_invite_ui.py`. |
| Uitnodiging: component-keuze opgeslagen, afgekeurde bestemming niet terug in de fout | **niet gemeten.** Vergt een uitnodigingsronde met een afgekeurd adres; buiten deze pass gelaten. |
| Keycloak: elke gebruiker met een adres bevestigt bij aanmaak | **niet gemeten.** De projectrealms op dit cluster (`test-ej1-sandboxed-local`, `jc-77j-sandboxed-local`, `rig-platform`) bevatten alle drie NUL gebruikers, dus er is geen `emailVerified` om te lezen. Meten vergt een aanmaakronde met een teamlid. |
| Het repositorypad blijft binnen de repo | **niet gemeten.** `test_repository_pad_binnen_de_repo.py` dekt de regel; een clustertoets zou dezelfde functie meten. |
| De ciphertext van een secret dat opnieuw gemaakt wordt blijft staan | **al gedekt** door `test_sandbox_secret_rollout.py`. |

## Wat de CLI-weg opleverde

Het plan verwachtte hiervan het meest: *"Een API-wijziging die de UI niet raakt maar de CLI
wel, valt vandaag nergens om."* Twee dingen vielen meteen om. Ze zaten allebei in de
**zad-cli-repository** en niet hier, dus ze zijn gemeld en niet gerepareerd.

**In de testfase opnieuw gemeten, en ze zijn allebei dicht.** De CLI is daarvoor uit
`github.com/RijksICTGilde/zad-cli` gehaald en geinstalleerd; `zadctl --version` zegt dan
**0.13.1**. De versie waar de eerste meting op stond, 1.0.0, bestaat niet als uitgave: de
CHANGELOG van de CLI schrijft bij `v0.10.0` dat het nummer 1.0.0 tijdens het schrijven is
teruggedraaid naar 0.10.0 voor de uitgave. Wat hieronder staat is dus wat de installeerbare
CLI doet.

**1. `connection-limit` was vanaf de CLI niet te zetten, en is dat nu wel.** Precies het veld
waar de hele speelruimte op rust. De eerste meting (losse bodies uit een bestand) gaf exit 2
op elk body waarin het veld voorkwam, met een melding die een heel ander veld aanwees
(`'scope' is 'shared', which is not one of shared, project`). Op 0.13.1 komt het veld gewoon
de deur uit, gemeten met `--dry-run` tegen de sandbox:

| aanroep | uitkomst |
|---|---|
| `service config set postgresql-database --target project --file <body met connection-limit: 42>` | exit 0, payload draagt het veld |
| `service config set postgresql-database --target project --set connection-limit=0` | exit 0, payload draagt het veld |
| dezelfde aanroepen zonder `--target` | geweigerd, met de reden: een dienst met meer dan een laag eist `--target` |

De CHANGELOG van de CLI draagt in zijn `Unreleased` een regel over precies deze vorm:
`anyOf: [X, null]`, waarmee een Pydantic-spec elk optioneel veld schrijft, telde als "twee
geaccepteerde vormen", en de klacht van de verkeerde tak werd gerapporteerd. Dat is dezelfde
vorm als `connection-limit`. Of de reparatie die hier gemeten is dezelfde is, is vanaf deze
kant niet vast te stellen.

**2. `zad component delete` heeft nu wel een uitweg voor een component in gebruik.** De API
draagt `confirm_in_use` en zegt over de 409 dat de body elke plek noemt waar de component nog
gebruikt wordt. De eerste meting zag alleen `--yes` en `--dry-run`. Op 0.13.1 zijn de vlaggen
`--name --force --yes --dry-run`, en `--force` belooft *"delete it even though something still
uses it, and removes those references"*.

**Wat dit over de toetsen zegt.** De keuze om de **serverkant** vast te pinnen en niet het
gebrek is de goede geweest, en dat is nu ook gemeten in plaats van beweerd: de twee toetsen
zijn niet rood geworden van de reparaties aan de CLI-kant. Het document moet beide scopes
blijven noemen en `confirm_in_use` moet blijven bestaan; dat is wat ze eisen.

Er is er wel een toets bijgekomen. De speelruimte werd alleen over de API gemeten omdat de
CLI het veld niet kon versturen, en die reden is weg:
`test_de_speelruimte_houdt_ook_als_de_cli_de_waarde_stuurt` loopt de weg af die het plan
vroeg, als paar (een te lage waarde raakt het projectbestand niet, een geldige wel).

**Waarneming zonder eigenaar.** De grenzen van `connection-limit` (1 tot 500) staan NIET in
het OpenAPI-document: het veld is daar een kale `integer`. Een client kan de speelruimte dus
niet vooraf kennen en komt er pas achter door een weigering. Dat is verdedigbaar (de grens
staat op een plek, zoals de feature-doc eist) maar het betekent wel dat elke client die het
vooraf wil weten, het moet raden.

## De stand van de nieuwe toetsen

Alles gemeten tegen build `65b90336` op het sandboxcluster.

| bestand | uitslag |
|---|---|
| `test_sandbox_zad_cli.py` | 5 / 5 |
| `test_sandbox_foutmeldingen.py` | 8 / 8 |
| `test_sandbox_migratie_006.py` | 5 / 5 |
| `test_sandbox_kloon.py` | 5 / 5 |
| `test_sandbox_registry_pull.py` | 3 / 3 |
| `test_sandbox_speelruimte.py` | 6 van de 7 |

De zeven in `test_sandbox_speelruimte.py` zijn er zes geweest: de zevende,
`test_de_speelruimte_houdt_ook_als_de_cli_de_waarde_stuurt`, kwam er na `sandbox-release`
bij en heeft dus geen clusteruitslag. Precies de toets die het plan vroeg, en precies de
toets die nog niemand op een cluster heeft zien draaien. Wie de sandbox als volgende claimt,
draait hem.

### Opnieuw gedraaid op 25 september, tegen `65bcf728`

De tabel hierboven is van build `65b90336`, en de reworkrondes daarna hebben de TOETSEN zelf
nog gewijzigd. Wat sindsdien opnieuw tegen het cluster gedraaid is (dezelfde serverbuild,
nieuwe toetscode):

| bestand | uitslag |
|---|---|
| `test_sandbox_migratie_006.py` | 5 / 5 |
| `test_sandbox_foutmeldingen.py` | 7 van de 8 |
| `test_sandbox_speelruimte.py` | 6 van de 7 |

De drie modules die de zad-cli nodig hebben (`zad_cli`, `kloon`, `registry_pull`) en de twee
losse CLI-toetsen sloegen over: de CLI zit in een eigen repository en stond niet op deze
machine. `test_de_speelruimte_houdt_ook_als_de_cli_de_waarde_stuurt` wacht daarmee nog steeds
op een gang; dat is niet een tekort van deze ronde maar van de omgeving waarin hij draaide.

De backfill leverde deze keer **3 rijen tegen 3 uit de oude meting**. De eerdere gangen zagen
1, 0 en 0 rijen (zie de fixture `taken`), dus die vergeleken de gelijkheid grotendeels op een
lege verzameling. Nu is hij op een niet-lege gemeten.

## Wat hieruit volgt

Wat buiten deze ronde valt en een eigenaar nodig heeft:

1. **Er is geen enkele dienst met een `grow_only`-veld.** Het mechanisme is gebouwd en
   getoetst, maar draait nergens. Wie de eerste declareert, hoort er de clustertoets bij te
   zetten die hier niet kon.
2. **De productiepin is niet herbouwbaar.** Dat is geen probleem zolang het gepubliceerde
   image bestaat, en een probleem op de dag dat dat niet meer zo is.
3. **De container registry van Forgejo op dit cluster geeft anoniem pull.** De aanname "hij
   weigert anoniem, want `GET /v2/` geeft 401" klopt niet: die 401 is de auth-UITDAGING die elke
   Docker-registry geeft. Doe je de tokendans die een client ook doet, dan levert
   `/v2/token?scope=repository:rig-admin/e2e-allservices:pull` ANONIEM een token op en komt de
   manifest met 200 terug (nagemeten met curl). Gevolg aan de kubelet-kant, twee keer gezien met
   een vaste EN met een eigen tag en `imagePullPolicy: Always`: het image was er "in 50ms"
   terwijl kubelet in dezelfde events `FailedToRetrieveImagePullSecret` meldde en het secret nog
   niet bestond. Een ontbrekend pull-secret is bij kubelet een WAARSCHUWING, geen fout.

   Wat daarmee nog wel gemeten is: de entry landt AGE-versleuteld in het projectbestand, en ZAD
   zet het `dockerconfigjson`-secret in de namespace met de juiste upstream en gebruiker. Wat
   niet: dat het zonder dat secret niet zou lukken. Wie die stap wil, heeft op deze sandbox een
   image nodig dat anoniem echt geweigerd wordt.

4. **Het pull-secret komt na de workload.** Het secret landt via de `_project`-applicatie en de
   pod via de deployment-applicatie; in de meting stond de pod er ruim twee minuten eerder. Dat
   herstelt zichzelf (kubelet probeert opnieuw), maar op een registry die anoniem weigert en een
   node zonder het image in zijn cache is dat zolang `ImagePullBackOff`.
