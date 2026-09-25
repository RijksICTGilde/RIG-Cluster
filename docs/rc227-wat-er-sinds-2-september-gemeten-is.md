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
| De registrykeuze staat bij de image, en het pull-secret landt | **gemeten**, zie `test_sandbox_registry_pull.py`. Uitkomst staat in de PR. |
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
| Speelruimte per veld, drie lezers uit een declaratie | **gemeten** over de API (`test_sandbox_speelruimte.py`): binnen de grenzen wordt opgeslagen, onder de ondergrens en boven de bovengrens geweigerd, de weigering noemt de grenzen, en het projectbestand blijft onveranderd. Niet over de CLI, want die kan dit veld niet versturen; zie "Wat de CLI-weg opleverde". |
| De guard sluit ook als het veld ontbreekt | **gemeten** als de servertoets op een niet-gedeclareerd veld. |
| Een laag die de dienst niet openzet | **gemeten en goed.** `connection-limit` staat op project en deployment; de componentlaag bestaat als route niet eens (`/services/postgresql-database/config/component/...` staat niet in het OpenAPI-document van de draaiende server). |
| Een `grow_only`-veld staat op precies een laag | **niet gemeten, en dat kan ook niet.** `grow_only` bestaat in het mechanisme, maar geen enkele dienst declareert een veld met die vlag: buiten `config_settings.py` komt het woord in `opi/` niet voor. Er is niets om op te richten. Zodra een dienst er een declareert, hoort er een clustertoets bij. |
| De connectielimiet is instelbaar | **gemeten**, dit is het veld waarop het bovenstaande gemeten is. |
| Een kale dienst is de standaard, ook als VORIGE versie | **niet gemeten.** Een uitspraak over twee versies van hetzelfde bestand; op een cluster alleen te maken door twee keer op te slaan, en dan meet je de opslagvolgorde. |

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
wel, valt vandaag nergens om."* Twee dingen vielen meteen om. Allebei zitten ze in de
**zad-cli-repository** en niet hier, dus ze zijn gemeld en niet gerepareerd.

**1. `connection-limit` is vanaf de CLI niet te zetten.** Precies het veld waar de hele
speelruimte op rust. Geisoleerd met vier bodies uit een bestand, op zad-cli 1.0.0:

| body | uitkomst |
|---|---|
| `{scope: shared}` | exit 0 |
| `{scope: shared, schemas: [{postfix: ''}]}` (zijn eigen `--generate-skeleton`) | exit 0 |
| `{scope: shared, connection-limit: 42}` | exit 2 |
| `{scope: shared, connection-limit: null}` | exit 2 |
| `{scope: project}` | exit 0 |
| `{scope: project, storage: 2Gi}` | exit 0 |

Elk body waarin `connection-limit` voorkomt wordt geweigerd door de client-side
schemacontrole van de CLI, met een melding die een heel ander veld aanwijst:

```
'scope' is 'shared', which is not one of shared, project.
```

`scope` is dus niet het probleem (de laatste twee rijen laten zien dat beide scopes gewoon
doorkomen), en de waarde die "niet in de lijst" zou staan staat er letterlijk in.
`connection-limit` is het enige veld in dat schema met de vorm `anyOf: [integer, null]`.

**2. `zad component delete` heeft geen uitweg voor een component in gebruik.** De API draagt
`confirm_in_use` en zegt over de 409 dat de body elke plek noemt waar de component nog
gebruikt wordt. De CLI kent alleen `--yes` en `--dry-run`, en drukt af:

```
✗ Conflict (HTTP 409): the resource is in a state that blocks this action.
```

De gebruiker leest een toestand en krijgt geen volgende stap.

Beide toetsen pinnen daarom de **serverkant** vast, niet het gebrek: het document moet beide
scopes blijven noemen, en `confirm_in_use` moet blijven bestaan. Een toets die de
CLI-weigering vastpint wordt rood zodra iemand de CLI repareert, en dat is precies verkeerd
om.

**Waarneming zonder eigenaar.** De grenzen van `connection-limit` (1 tot 500) staan NIET in
het OpenAPI-document: het veld is daar een kale `integer`. Een client kan de speelruimte dus
niet vooraf kennen en komt er pas achter door een weigering. Dat is verdedigbaar (de grens
staat op een plek, zoals de feature-doc eist) maar het betekent wel dat elke client die het
vooraf wil weten, het moet raden.

## Wat hieruit volgt

Twee dingen die buiten deze ronde vallen maar wel een eigenaar nodig hebben:

1. **Er is geen enkele dienst met een `grow_only`-veld.** Het mechanisme is gebouwd en
   getoetst, maar draait nergens. Wie de eerste declareert, hoort er de clustertoets bij te
   zetten die hier niet kon.
2. **De productiepin is niet herbouwbaar.** Dat is geen probleem zolang het gepubliceerde
   image bestaat, en een probleem op de dag dat dat niet meer zo is.
