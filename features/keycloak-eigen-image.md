# Een eigen Keycloak-image

De Keycloak van het platform draait op een image die wij zelf bouwen: de basisversie uit
quay met onze providers er al in. De pod haalt bij het starten niets meer op.

Bron: `images/keycloak-zad/Dockerfile`. Image: `ghcr.io/rijksictgilde/zad/keycloak-zad`.

## Wat erin zit

| Provider | Waar hij vandaan komt |
|---|---|
| `keycloak-relay-email-sender` | `keycloak-migration/relay-email-sender/`, gebouwd tijdens de imagebouw |
| `keycloak-saml-nameid-mapper` (vier SPI's) | `keycloak-migration/custom-mapper/`, gebouwd tijdens de imagebouw |
| `keycloak-nl-design-system` (thema) | release van MinBZK, opgehaald tijdens de imagebouw, op sha256 gecontroleerd |

De twee eigen jars komen dus uit de bron in deze repo. Een jar kan daarmee niet meer uit de
pas lopen met de code ernaast; dat is wat de byte-voor-byte-vergelijking op de ingecheckte
jar bewaakte, en die jar staat niet meer in git.

Het thema blijft extern. Het verschil is wanneer: hapert GitHub, dan faalt de BOUW, en dat
is iets anders dan een pod die niet opkomt.

## Waarom niet ophalen bij het starten

Aan de relay-emailSender hangt de startvlag `--spi-email-sender-provider`, en die maakt een
stille terugval op de standaardprovider onmogelijk, zie `features/keycloak-mail.md`. De provider
moet er dus zijn. Elke weg waarlangs die jar bij het starten van het netwerk komt, maakt het
inloggen van het hele platform afhankelijk van die download.

De eerdere oplossing zette alleen die ene jar in git en leverde hem via een ConfigMap. Dat
werkte, maar het is een rare vorm om artefacten aan te bieden, en het liet het thema en de
SAML-mapper nog wel bij elke podstart van github.com komen.

## De providers zitten er ingebakken

`kc.sh build` staat in de Dockerfile. Keycloak neemt providers pas op bij een augmentatie,
en zonder die stap doet de pod dat werk bij elke start alsnog: ruim negen seconden, gemeten
in de proefopstelling van deze wijziging.

Die stap doet nog iets. `kc.sh build --spi-email-sender-provider=zad-relay` FAALT op een
provider-id dat niet oplost ("Failed to find provider ... for emailSender", exit 1), dus de
canarie uit RC-158 zingt tijdens de imagebouw in plaats van in een pod op productie.

### Welke vlag waar hoort

De bouw alleen is niet genoeg: de pod moet er ook met `--optimized` op starten. Zonder die
vlag vergelijkt Keycloak bij elke start zijn configuratie met wat de image draagt, en dan
herbouwt hij alsnog stil ("Changes detected in configuration", ~9 seconden augmentatie).

De oorzaak zit in wat `kc.sh build` VASTLEGT. Van de opties die wij meegeven staan na die
stap deze vier in `META-INF/keycloak-persisted.properties`:

```
kc.db=postgres
kc.features=admin-api
kc.health-enabled=true
kc.metrics-enabled=true
```

Keycloak schrijft er zelf nog een blok bij (`kc.optimized`, `kc.quarkus-properties-enabled`
en een reeks `quarkus.*`), dus wat hierboven staat is niet het hele bestand.

Geen enkele `spi-`optie staat erin, ook niet als je hem aan `kc.sh build` meegeeft. Elke
`spi-...-provider` of `spi-...-enabled` die de pod noemt leest daarom als een verschil met de
image, en dan herbouwt Keycloak. Die vlaggen zijn niet te missen (`--spi-email-sender-provider`
wijst de eigen mailverzender aan, en zonder hem verstuurt de standaardprovider), dus de enige
uitweg is `--optimized`.

Daaruit volgt een verdeling met voor elke optie precies een plek:

| Soort | Waar hij staat | Waarom daar |
|---|---|---|
| bouwoptie (`db`, `features`, `health-enabled`, `metrics-enabled`) | `kc.sh build` in de Dockerfile | wordt vastgelegd in de image. Met `--optimized` WEIGERT Keycloak te starten als er zo'n vlag bij de args staat ("Build time option: '--db' not usable with pre-built image and --optimized"), ook bij een gelijke waarde |
| `spi-...`-optie | de args en de env van de deployment | wordt niet vastgelegd, dus alleen daar doet hij iets. Gemeten: met de themavlag in `kc.sh build` en niet bij de start komt de welkomstpagina zonder nl-design-system op |
| runtime-optie (`proxy-headers`, `hostname*`, `db-url`) | de env van de deployment | daar hoort hij, en nergens anders ook |

`--spi-email-sender-provider=zad-relay` staat op twee plekken, en dat is de enige uitzondering:
in de deployment omdat de waarde daar GELDT, en in `kc.sh build` als canarie die de bouw laat
falen. Dat ze dezelfde id noemen pint
`test_de_bouwcanarie_noemt_dezelfde_verzender_als_de_pod`.

**Wat `--optimized` niet oplost.** Een BOUWoptie die als omgevingsvariabele afwijkt, wordt niet
geweigerd maar alleen gemeld ("The following build time non-cli options have values that differ
from what is persisted"). Daarom staat er geen bouwoptie meer in de env van de deployment, en
bewaakt `test_geen_bouwoptie_staat_in_de_omgeving` dat.

## De pin staat per omgeving

| Plek | Wat er staat |
|---|---|
| `controller/base/deployment.yaml` | de tag die PRODUCTIE draait |
| `controller/overlays/sandboxed-local/kustomization.yaml` | een eigen `newTag` |
| `controller/overlays/odcn/kustomization.yaml` | alleen het pad naar de rcr-spiegel, plus het pull-secret |

De odcn-overlay erft de tag van de basis. Zou hij een eigen `newTag` dragen, dan kan die
uiteenlopen met wat er als "de tag die productie draait" in de basis staat, en dan is er geen
plek meer die het antwoord geeft.

Het rcr-pad moet in git staan. ODCN kan ghcr.io niet bereiken en herschrijft het image bij
admission; staat de herschreven vorm niet in git, dan blijft de applicatie in ArgoCD eeuwig
OutOfSync. Die proxy vraagt bovendien authenticatie, ook voor een image dat op ghcr publiek
is, vandaar `ghcr-rig-robot-pull-secret`.

De begintoestand van die twee plekken is geen tag maar een woord: `promoveer-eerst` in de
basis, `bouw-eerst` in de sandbox-overlay. Een verzonnen versienummer zou eruitzien als een
image die te halen valt, en dat blijkt dan pas wanneer de pod opnieuw wordt aangemaakt.
Zodra er gebouwd of gepromoveerd is, staat er een echte tag; `publish-keycloak-image` en
`pin-keycloak-image` schrijven alleen het stuk achter de dubbele punt.

## Bouwen

De image gaat ALTIJD naar GHCR, ook die voor de sandbox. Dat is niet alleen eenvormigheid.
Draaide de sandbox een lokaal gebouwde image en productie later een door CI gebouwde met
dezelfde tag, dan zijn dat twee verschillende artefacten. Dan is de regel "promoveren is
dezelfde tag als die op de sandbox draaide" niets waard.

**In CI**, per image te kiezen: de workflow `Docker - Supporting Images`
(`.github/workflows/docker-images.yml`), image `keycloak-zad`. Die levert drie tags: een
CalVer-tag, `sha-<commit>` en `latest`.

**Lokaal, voor de sandbox:**

```bash
docker login ghcr.io            # eenmalig, token met write:packages
task publish-keycloak-image     # bouwen, naar ghcr pushen, de sandbox-overlay pinnen
task sandbox:sync               # uitrollen
```

Dezelfde vorm als `publish-operations-manager`: de taak rekent de tag uit, bouwt en pusht via
de gedeelde taak `docker-build-and-push` (dezelfde twee platforms als CI), en roept daarna
`pin-keycloak-image` aan. Het werk staat in de Taskfile en niet in een eigen script, zodat
een CI-job later dezelfde taak kan aanroepen.

De tag is een CalVer, in de vorm die de operations-manager ook draagt:
`{YYYY}.{MM}.{DD}.{HHMM}-{korte commit}`, met `-dirty` erachter als de werkboom niet schoon
is. De taak waarschuwt daar ook op: die commit beschrijft dan niet wat je publiceert.

Er is GEEN controle of er wel iets te bouwen valt. Elke aanroep bouwt en pusht een nieuwe
tag, dus "geen release zonder aanleiding" is een beslissing van de mens, net als bij de
operations-manager.

De builder komt van `sandbox:build-builder`, en niet uit een eigen naam zoals
`publish-operations-manager` die meegeeft: die builder draagt de geheugengrens van de
gedeelde dev-server en het host-netwerk dat deze bouw daar nodig heeft.

`pin-keycloak-image` schrijft standaard ALLEEN de `newTag` van de sandbox-overlay. De basis
en de odcn-overlay blijven staan, dus een proef in de sandbox is geen uitrol naar ODCN. Die
taak drukt af wat je moet committen, welk deploycommando erbij hoort, en hoe je terugrolt. Hij
is ook los aan te roepen, bijvoorbeeld om een tag van CI op de sandbox te zetten:
`task pin-keycloak-image IMAGE_TAG=<tag>`. Met `TARGET=base` schrijft dezelfde taak de basis,
en dat is de promotie; zie hieronder.

### Het pakket moet publiek staan

De kind-node van de sandbox pullt ANONIEM uit ghcr.io en heeft daar geen pull-secret voor.
Ghcr maakt een pakket na de eerste push PRIVAAT, en dan strandt die node op

```
failed to fetch anonymous token: unexpected status from GET request to
https://ghcr.io/token?scope=repository%3Arijksictgilde%2Fzad%2Fkeycloak-zad%3Apull: 403 Forbidden
```

Dat is de fout die na de eerste push van dit pakket ook echt kwam. Het pakket staat inmiddels
op publiek (Package settings > Change visibility), zoals de `minbzk/base-images`-pakketten die
de sandbox al haalt; een anoniem token voor `rijksictgilde/zad/keycloak-zad` lukt nu. Ghcr
geeft datzelfde 403 voor een pakket dat privaat staat en voor een pakket dat niet BESTAAT, dus
die fout zegt alleen: anoniem halen kan nu niet.

`pin-keycloak-image` stelt daarom voor het pinnen dezelfde twee vragen als de node (eerst een
anoniem token, dan het manifest van die tag) en PINT NIET als een van beide faalt. Zo strandt
een privaat pakket in de taak, en niet in een pod die pas bij de volgende herstart opkomt.

Blijft het pakket privaat, dan is het alternatief een pull-secret in de sandbox-overlay plus
het aanmaken ervan in de sandboxsetup, zoals de odcn-overlay dat heeft. De sandbox heeft
vandaag geen enkel registry-credential; dat is dan een eigen wijziging.

## Toetsen

```bash
task test-keycloak-relay-email-sender    # mvn test van de ene module
task test-keycloak-custom-mapper         # mvn test van de andere
```

CI draait `mvn -B test` voor allebei bij elke pull request (job `keycloak-providers`).

De kant die de image en de manifesten bewaakt zit in de Python-suite:
`tests/test_keycloak_eigen_image.py` en `tests/test_keycloak_relay_email_sender.py`.

Op een draaiend cluster is de log van een VERSE pod de toets:

```bash
kubectl -n rig-system logs deployment/keycloak | grep -E "Changes detected|Quarkus augmentation"
```

Dat hoort niets op te leveren. Levert het wel iets op, dan noemt de pod een bouwoptie die de
image niet draagt, of mist `--optimized`. Een pod die opkomt is op zichzelf GEEN bewijs dat de
providers er zijn: met `--optimized` start Keycloak ook op een provider-id dat niet bestaat
(gemeten). Dat bewijs komt uit de imagebouw, en uit de providers in de log.

## Promoveren naar productie

Promoveren is het verplaatsen van precies de tag die op de sandbox heeft gedraaid, niet
opnieuw bouwen. Een herbouw levert een ander artefact op, en dan is het bewijs van de
sandbox niets waard.

Het is dezelfde handeling als het pinnen van de sandbox, met een ander doel, en dus dezelfde
taak:

```bash
task publish-keycloak-image              # bouwt, pusht, pint de sandbox-overlay
task sandbox:sync                        # rol uit en toets op de sandbox
task pin-keycloak-image TARGET=base      # zet die tag in de basis, zonder herbouw
git commit ...                           # een bewuste wijziging in git
```

Er hoort geen `IMAGE_TAG` bij `TARGET=base`: de tag komt uit de sandbox-overlay, want de basis
mag alleen een tag dragen die op de sandbox is getoetst. Geef je er toch een mee, dan moet het
die tag zijn; een andere wordt geweigerd.

### De tag in de basis kan niet uiteenlopen met wat er gepusht is

Geen van beide tags wordt met de hand geschreven, en dat is de hele borging:

| Plek | Wie schrijft hem | Wanneer |
|---|---|---|
| sandbox-overlay | `task pin-keycloak-image` | pas nadat de tag zonder inlog terug te lezen was |
| basis | `task pin-keycloak-image TARGET=base` | door de tag uit de sandbox-overlay over te nemen, zonder te bouwen |
| odcn-overlay | niemand | hij draagt geen eigen tag en erft die van de basis |

De promotie weigert een `-dirty`tag: die hoort bij bron die niet in git staat, en dan is op
productie niet na te gaan wat er draait. De CalVer-tag die CI uitrekent (`{YYYY}.{M}.{patch}`,
een andere vorm dan die van de taak) wordt in dezelfde run gepusht en komt in geen enkel
manifest terecht; dat was de weg waarlangs de basis ooit een `2026.9.1` droeg die nergens
bestond.

## Regels voor uitbrengen

- **De jars blijven eigen artefacten.** De image bundelt ze, hij vervangt ze niet. Elke
  module houdt zijn eigen pom, zijn eigen versienummer en zijn eigen `mvn test`.
- **Geen Keycloak-release zonder aanleiding.** Een nieuwe image-tag hoort bij een gewijzigde
  jar of een gewijzigde Keycloak-basisversie, en anders niet.
- **De GitHub-release van de mapper is gestopt.** Er worden geen nieuwe assets meer
  gepubliceerd nu de image de bron is. De bestaande `v1.1.0` blijft staan, zodat een oud
  manifest of een terugval er niet op stukloopt.
- **Een Keycloak-upgrade is nu ook een imagebouw.** De basisversie staat in de Dockerfile en
  moet gelijk blijven aan `keycloak.version` in allebei de poms; een toets pint dat. Wat er
  bij zo'n upgrade gemeten moet worden staat in `features/keycloak-26-upgrade.md`.
