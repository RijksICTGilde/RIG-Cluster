# Een service declareert zijn speelruimte

Status: plan, 1 september 2026. Aanleiding: twee taken die los van elkaar begonnen kwamen op hetzelfde patroon uit. De connectielimiet moet instelbaar worden per project en per deployment, en een project met een eigen databasecluster moet zijn geheugen en volumegrootte kunnen zetten. In beide gevallen is de vorm identiek: **de service bepaalt wat verantwoord is, het project kiest daarbinnen een waarde, en iets moet controleren dat die keuze binnen de perken blijft.**

Dat mechanisme bestaat nog niet. Vandaag staat zo'n grens als een los getal in een pydantic-model, of hardgecodeerd in een connector, of nergens. Wie er een tweede geval bij bouwt, bouwt de derde variant.

Dit plan hoort onder [de-connectielimiet-wordt-instelbaar.md](de-connectielimiet-wordt-instelbaar.md) en [van-gedeeld-naar-een-eigen-databasecluster.md](van-gedeeld-naar-een-eigen-databasecluster.md). **De volgorde is: eerst dit, dan de limiet, dan de migratie.** Andersom ontwerp je een generiek mechanisme vanuit één geval, en dat komt zelden goed uit.

**Scope.** Dit plan raakt `opi/services/catalog/base.py` en de gedeelde laag eromheen (`config_schema.py`, de forms- en validatielaag). Het verandert **geen enkel bestaand gedrag**: elke service die vandaag niets declareert houdt precies wat hij heeft. Het voegt een declaratie toe die twee volgende taken kunnen gebruiken.

## Wat er nu is, gemeten

### De lagen bestaan al, de declaratie niet

`ConfigLayer` kent `PROJECT`, `COMPONENT`, `DEPLOYMENT` en `DEPLOYMENT_COMPONENT`. Tien services in `opi/services/catalog/` declareren config op meer dan één laag, en `cross_domain_access` heeft gebruikersgerichte editables op de deploymentlaag via `config_path(ConfigLayer.DEPLOYMENT, ServiceType.X, "config", ...)`.

Een service kan dus al zeggen *welke velden* hij op welke laag heeft. Wat hij niet kan zeggen is **wat een geldige waarde is** en **wat er gebeurt als twee lagen allebei iets zeggen**.

### Grenzen liggen nu verspreid of nergens

Drie voorbeelden uit dezelfde service-familie:

| grens | waar hij nu staat |
|---|---|
| connectielimiet 20 | hardgecodeerd in `opi/connectors/postgres.py:499`, in de connectorlaag |
| `storage` standaard `1Gi` | als `Field(default=...)` in `catalog/shared/postgres.py` |
| geheugen limiet `512Mi` | idem, maar zonder enige boven- of ondergrens |

Van die drie kent alleen de tweede een standaard, en geen enkele kent een maximum. Een project kan vandaag `storage: 500Gi` opgeven en niets houdt dat tegen tot het cluster het weigert.

### Er is geen samenvoegregel

Waar een service op twee lagen config heeft, is er nergens één plek die zegt wie wint. `postgresql-database` lost dat vandaag op door de lagen verschillende dingen te laten dragen: de projectlaag draagt gebruikerskeuzes, de deploymentlaag draagt clone-state die `revision_manager` schrijft. Dat werkt zolang de sets niet overlappen. Zodra hetzelfde veld op twee niveaus mag staan, en dat is precies wat de twee volgende taken vragen, is die ontwijking op.

## Het model

Een service declareert per instelbaar veld een **speelruimte**: de ondergrens, de bovengrens en de standaard. Die drie zijn een platformbeslissing, dus ze staan hardgecodeerd in het servicepakket en zijn van buitenaf niet op te rekken.

Een projectbestand levert een **waarde**, op de laag waar dat is toegestaan. De waarde is gebruikersinvoer en wordt gevalideerd tegen de declaratie van de service, niet tegen een los getal dat toevallig in een model staat.

```
service declareert:   min 1,  max 100,  standaard 20
                              |
projectbestand kiest:         60          <- gevalideerd tegen bovenstaande
deployment overschrijft:      80          <- idem
                              |
connector krijgt:             80
```

### Bij een gedeelde service is het hetzelfde blok, met één verschil

Dit is gewoon een serviceconfigblok zoals er al meer zijn, en het hoort er ook niet anders uit te zien. Eén ding is wel bijzonder: bij een gedeelde service beschrijft dat blok hoe die service zich **voor dit project** gedraagt op iets dat het project niet bezit. Bij een eigen cluster zet je eigenschappen van je eigen ding; bij de gedeelde instantie vraag je een plak van iets dat je met tachtig anderen deelt.

Dat maakt de vorm niet anders, wel het belang van de grenzen. Een onzinnige waarde op je eigen cluster raakt alleen jou. Dezelfde waarde op de gedeelde instantie gaat ten koste van de rest. Vandaar dat de speelruimte hardgecodeerd bij de service hoort en niet iets is dat een project kan oprekken.

### Drie regels die daaruit volgen

**Eén bron per grens.** Het model, de wizard en de foutmelding putten alle drie uit dezelfde declaratie. Zet je de bovengrens als `le=100` in een pydantic-veld, dan staat hij binnen een maand op drie plekken uiteen te lopen.

**Specifieker wint.** Deployment boven project, project boven de standaard van de service. Eén functie, niet per service opnieuw bedacht.

**Wat een service niet declareert, is niet instelbaar.** Geen veld dat per ongeluk openstaat omdat het toevallig in een model zit. Dat is meteen het antwoord op "mag een gebruiker dit wel zelf zetten": alleen wat expliciet is opengezet.

## Wat er moet gebeuren

Vier stappen. De eerste drie zijn nodig voordat de andere twee plannen kunnen beginnen; de vierde mag later.

### 1. De declaratie

Een manier voor een service om per veld ondergrens, bovengrens en standaard op te geven, plus op welke lagen het veld gezet mag worden. Woont bij de service, in het servicepakket.

Het veldenbestek is breder dan alleen de connectielimiet, en dat bepaalt hoe de declaratie eruit moet zien. Voor `postgresql-database` gaat het minstens om deze velden, die op één na allemaal al in `catalog/shared/postgres.py` staan:

| veld | soort | wat een grens hier betekent |
|---|---|---|
| connectielimiet | geheel getal | ondergrens, bovengrens |
| `instances` | geheel getal | ondergrens, bovengrens |
| `resources.requests.cpu` / `.limits.cpu` | Kubernetes-hoeveelheid (`100m`, `2`) | ondergrens, bovengrens, en requests mag de limits niet overschrijden |
| `resources.requests.memory` / `.limits.memory` | Kubernetes-hoeveelheid (`256Mi`, `2Gi`) | idem |
| `storage` | Kubernetes-hoeveelheid | ondergrens, bovengrens, en **alleen omhoog** bij een wijziging |
| `image`, `registry` | tekst | geen min of max, maar een toegestane verzameling of patroon |

Daaruit volgen drie soorten grens, en niet één:

- **getal**: ondergrens en bovengrens, gewone vergelijking;
- **hoeveelheid**: `100m` en `2` zijn allebei cpu, `512Mi` en `1Gi` allebei geheugen. Vergelijken vraagt om ontleden naar een grondeenheid, niet om tekstvergelijking. Doe dat op één plek, want dit is een klassieke bron van fouten waarbij `1Gi` kleiner lijkt dan `512Mi`;
- **tekst uit een toegestane verzameling**: voor `image` en `registry`.

Meer soorten dan deze drie zijn er nu niet nodig. Verzin er geen vierde bij voor iets wat nog niemand vraagt.

### 2. De samenvoegregel

Eén functie die uit de servicestandaard en de waarden op de toegestane lagen één effectieve waarde maakt, volgens "specifieker wint". Op één plek, gedeeld door alle services.

### 3. De validatie

Bij het inlezen van een projectbestand wordt elke opgegeven waarde getoetst aan de declaratie van zijn service. Buiten de speelruimte is een leesbare fout, geen stille afkapping en geen waarde die pas veel later bij het aanmaken van een resource omvalt.

Dit is het punt waar dit plan beveiligingsrelevant wordt: hier wordt gebruikersinvoer gecontroleerd voordat hij ergens terechtkomt.

### 4. De wizard leest de declaratie

Een veld met een gedeclareerde speelruimte kan zijn eigen invoercontrole en helptekst daaruit halen, in plaats van dat iemand de grenzen een tweede keer in een visualizer overschrijft.

## De toets

- een service die niets declareert gedraagt zich exact als vandaag: dat is de regressietoets over de hele catalogus;
- een waarde binnen de speelruimte komt ongewijzigd bij de connector aan;
- een waarde buiten de speelruimte geeft een leesbare fout bij het inlezen van het projectbestand, niet pas bij het aanmaken van de resource;
- staat hetzelfde veld op twee lagen, dan wint de specifieke, en er is één test die alle vier de combinaties afgaat (geen, alleen project, alleen deployment, beide);
- een veld dat de service niet declareert is niet instelbaar, ook niet door het handmatig in het projectbestand te zetten;
- de bovengrens staat op precies één plek: `grep` naar het getal levert één treffer in het servicepakket op en niets in een model, een visualizer of een validator;
- `1Gi` wordt herkend als groter dan `512Mi` en `2` als groter dan `100m`: hoeveelheden worden ontleed en niet als tekst vergeleken, met een test die precies die twee paren afgaat;
- `storage` verlagen wordt geweigerd met een leesbare fout, ook als de nieuwe waarde binnen de gedeclareerde grenzen valt;
- een `image` buiten de toegestane verzameling wordt geweigerd bij het inlezen, niet pas als ArgoCD hem niet kan pullen;
- de wizard toont dezelfde grenzen als de validatie afdwingt, aantoonbaar doordat beide dezelfde declaratie lezen.

## Waar op te letten

**Dit plan mag niets veranderen.** De waarde ervan zit erin dat de twee volgende taken erop kunnen bouwen, niet in wat het zelf oplevert. Verandert er gedrag van een bestaande service, dan is er iets te veel ontworpen.

**Twee gevallen is genoeg, en ook precies genoeg.** De connectielimiet en de clusterinstellingen hebben dezelfde vorm en zijn samen genoeg om het mechanisme goed te krijgen. Eén geval is te weinig, en wachten op een derde is uitstel. Bouw voor deze twee en niets meer.

**`image` en `registry` staan vandaag al open, als vrije tekst.** Dit plan zet daar niets nieuws open; het geeft die twee velden een gedeclareerde verzameling in plaats van niets. Dat is winst, geen extra risico. Wel praktisch: het ODCN-cluster herschrijft images bij admission naar `rcr`, dus een waarde die daar niet doorheen komt levert nu een eeuwige OutOfSync-lus op. Een toegestane verzameling verandert dat in een leesbare fout bij het inlezen.

**Alleen omhoog voor een volume.** `storage` verkleinen kan een PVC niet. Een grens die alleen zegt "tussen 1Gi en 100Gi" laat een verkleining door die daarna stilletjes niets doet of de uitrol laat vastlopen. Voor dat veld hoort de regel "niet lager dan wat er nu staat" bij de validatie.

**Grenzen zijn geen quotum.** Een bovengrens per veld voorkomt dat één project een onzinnige waarde opgeeft. Hij voorkomt niet dat honderd projecten allemaal het maximum vragen. Voor gedeelde middelen, en de verbindingen op de gedeelde database zijn daar het voorbeeld van, is een quotum per project een aparte vraag die dit plan niet beantwoordt.

**Validatie is nog geen autorisatie.** Dit plan controleert of een waarde geldig is, niet of degene die hem invulde dat mocht. Die rechtencheck staat als vervolgtaak in het connectielimiet-plan en verandert daar niets aan.

**Alles wat ik hier een naam geef is een voorstel.** Er staan geen bestaande identifiers in dit plan voor de nieuwe declaratie. Kies de namen bij het bouwen; laat mijn formuleringen niet doorlekken naar velden of functies.

## Wat hierna nodig is

Als dit staat, worden de twee andere plannen kleiner. De connectielimiet is dan een declaratie van drie getallen plus een aansluiting op de connector. En het migratieplan hoeft niet zelf te bedenken hoe clusterinstellingen uit een projectbestand gevalideerd worden, want dat is dan een opgelost probleem.
