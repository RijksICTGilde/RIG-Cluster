# Eigen container registries (`image-registries`)

Wie een image draait uit een eigen private registry, vult in wat alleen de afnemer weet:
de registry en een token. Wat er technisch onder gebeurt verschilt per cluster, en dat
verschil merkt de afnemer niet.

```
afnemer geeft:  upstream + token (+ gebruikersnaam waar die telt)  (eenmalig, projectniveau)
                per component: welke registry hoort bij deze image

ZAD doet op een cluster met internettoegang (kind, sandbox):
                dockerconfigjson-secret in de namespace, image blijft ongewijzigd

ZAD doet op een cluster achter een Quay-operator (ODCN):
                Organization met proxyCache + credentials,
                image herschreven naar rcr.rijksapps.nl/<org>/<repo>:<tag>
```

Het projectbestand is op beide platforms identiek. Alleen de provisioning verschilt.

## Hoe je het gebruikt

In de dienstconfig van het project staan de registries:

```yaml
services:
  - image-registries:
      config:
        registries:
          - name: code-overheid
            upstream: code.overheid.nl/robbert.uittenbroek
            username: robbert.uittenbroek
            password: <AGE>
```

Een entry draagt precies een van twee manieren om te pullen: `username` plus `password`, of
een `secretName` naar een dockerconfigjson-secret dat het platform zelf neerzet. Geen van
beide of allebei weigert `RegistryEntry` (`_has_exactly_one_way_to_pull`), dus het formulier
en de API op dezelfde save-poort. Zonder die regel kwam een entry zonder token overal
doorheen en schreef de backend stil geen pull-secret, waarna de afnemer het pas merkte aan
een pod die niet kon pullen. De tak in `backends.py` die dat vroeger stil deed is nu
onbereikbaar en blaast op (`MissingRegistryCredentialsError`) als hij er toch komt; op ODCN
wordt er dus ook geen proxy-organisatie zonder credentials meer aangemaakt. De twee
entries die de vloot heeft (`algor-odc` met token, `dp-bn7` met `secretName`) voldoen
allebei.

De weigering noemt wat er mist: een entry met een gebruikersnaam maar zonder token krijgt
"Vul een token in bij de gebruikersnaam", niet de kale vraag om allebei. De
generieke foutopbouw in `project_validation.py` zet er de plek voor (`registries, nummer 2: ...`,
geteld vanaf 1), zodat je bij meer registries weet welke je moet repareren.

### De gebruikersnaam is optioneel, het token niet

Wat `username` betekent verschilt per registry: bij ghcr.io doet de waarde er niet toe zolang
het token klopt, bij Docker Hub moet het de accountnaam zijn en bij Quay de robotnaam. Dat
verschil kan een formulier niet weten en de afnemer hoeft het niet te weten, dus het veld is
optioneel (RC-187, een herziening van de eerste vorm waarin hij verplicht was).

Leeg laten verandert niets aan het projectbestand: er komt geen waarde in, ook niet bij het
opslaan en ook niet in de migratie. De gebruikersnaam voor de dockerconfigjson ontstaat pas op
het moment dat het manifest wordt gebouwd, uit `PULL_USERNAME_PLACEHOLDER` (`naming.py`,
`x-access-token`) via `_pull_username()` in `backends.py`. Dezelfde regel als bij de RCR-URL en
de secretnaam: het projectbestand draagt alleen wat de afnemer heeft ingevuld, de rest is een
berekening. Er MOET iets staan omdat een `kubernetes.io/dockerconfigjson` per registry een
`auth` van `base64(gebruikersnaam:wachtwoord)` draagt: er is geen veld voor alleen een token.

Een registry die wel een echte naam eist heeft geen poort die hem bij een lege gebruikersnaam
tegenhoudt. De tokentoets (`enforcers.py`) praat echt met de registry, met precies het paar
dat de backend daarna schrijft, maar alleen onder twee voorwaarden. Hij draait in elke
formulierflow waar het registryblok in zit: de create- en edit-wizard (bij de stap vooruit en
bij de eindinzending), de dienstenmodal en de modal van het blok. Hij draait niet in de
componentmodal (die heeft alleen de componentsectie) en niet via de API (ook niet
`POST .../registries/by-credentials`). En hij toetst alleen tegen images onder de upstream die
in de samengevoegde data van de flow staan, zie de alinea over de tokentoets verderop. In de
edit-wizard en de modals staan de bestaande componenten daar altijd in, ook als de flow ze niet
toont; alleen bij de eerste stap vooruit in de create-wizard nog niet. Een component dat later
via de componentmodal of de API bijkomt wordt niet getoetst, en zo'n entry loopt dan pas bij
de pull vast. Faalt de toets terwijl het veld leeg was, dan noemt de melding dat de
gebruikersnaam waarschijnlijk nodig is in plaats van alleen te zeggen dat het token niet werkt
(`_access_denied_message`).

Het tokenveld in het formulier is een `WidgetType.PASSWORD`: afgeschermd op het scherm.

De naam is vrije tekst plus een afgeleide verwijzing, net als bij het project zelf:

```yaml
        registries:
          - name: code-overheid          # de verwijzing, afgeleid en daarna bevroren
            display-name: Code Overheid  # wat de afnemer typte
```

`display-name` mag ontbreken (elke registry van voor RC-187 heeft alleen een `name`); dan
is de verwijzing zelf het label op het scherm. `name` mag ook ontbreken, maar alleen met een
label ernaast: `generate_missing_values` leidt de slug er dan uit af met `registry_slug()`,
uniek binnen het project, en laat een bestaande slug met rust -- hij is de verwijzing vanaf
componenten en hij zit in de naam van het dockerconfigjson-secret, dus een gewijzigd label
mag hem niet meenemen. Die haak draait op allebei de schrijfwegen: de portal via `post_merge`
van de configsectie, de API via `registry.generate_missing_values`.

Bij een component staat alleen een verwijzing bij naam, en alleen als er iets te verwijzen
valt:

```yaml
components:
  - name: demo                                    # private image
    image: code.overheid.nl/robbert.uittenbroek/zad-deployment-demo:0a611d9d
    services:
      - reference: image-registries
        config:
          registry: code-overheid

  - name: proxy                                   # publieke image
    image: nginx:alpine                           # dienst niet aangevinkt, dus niets
```

"Publieke registry" is een non-waarde: geen vermelding, geen sleutel. Een deployment mag de
keuze overschrijven met dezelfde dienstvermelding onder `deployments[].components[].services`.

### De keuze staat bij de image, en de keuze IS de selectie

Zodra het project minstens een registry heeft, staat er bij elk component een keuzeveld
**Registry**, direct achter het image-veld, zonder dat de dienst bij dat component is
aangevinkt. Waar dat veld staat bepaalt het FORMULIER (`COMPONENT_IMAGE_SLOT` in
`opi/forms/layout.py`), wat erin komt bepaalt de dienst (`slot=` op zijn layoutknoop).
Een dienst die geen slot noemt landt nog steeds onderaan de componentvorm.

Er is geen aan/uit voor deze dienst op componentniveau: hij staat niet in het rijtje vinkjes.
Twee knoppen voor dezelfde beslissing zou betekenen dat we moeten bedenken wat een
aangevinkte dienst met waarde "publiek" betekent, en wat een uitgevinkte dienst met een
registry erin betekent, en die twee regels lopen uit elkaar. Een keuzelijst met een
expliciete "geen"-optie is net zo expliciet als een vinkje, alleen met meer opties.

Drie richtingen, en ze gelden voor het formulier en voor de API:

| Wat de afnemer doet | Wat er gebeurt |
|---|---|
| een registry kiezen | de dienstvermelding wordt gematerialiseerd op dat component |
| "Publieke registry" kiezen | een bestaande vermelding gaat weg; er wordt niets geschreven |
| niets kiezen | er verandert niets; afwezig BETEKENT publiek |

Dat is precies de val waar `instructions/services.md` voor waarschuwt -- het `{K}`-padfilter
materialiseert een dienst als bijwerking, dus een default wordt stil een selectie -- en die
willen we hier WEL, maar alleen in de ene richting.

De regel staat een keer, als `component_selection_follows_config` op de dienst, met twee
lezers: `FilteredServiceOptionsProvider` laat de dienst uit het vinkjesrijtje, en
`ServiceAdapter.remove_service_config` haalt bij het wissen de vermelding weg in plaats van
hem terug te zetten naar een kale naam. Aan de formulierkant doet
`_prune_service_map_entry` (`opi/forms/editables/processor.py`) hetzelfde zodra de laatste
waarde uit het blok verdwijnt.

Waar het veld VERSCHIJNT volgt uit zijn eigen keuzelijst: `hidden_without_options` op de
editable vraagt dezelfde provider die de widget vult en waar `values_must_exist` een
opgeslagen waarde tegen houdt. Geen tweede voorwaarde ernaast die eruit kan lopen. Heeft het
project geen registries, dan blijft de componentvorm precies zoals hij was -- de toestand van
47 van de 49 projecten.

### De weg terug is geen stille weg

Het keuzeveld verdwijnt als de laatste registry weggaat OF als de dienst op projectniveau
wordt uitgezet, maar de verwijzing in het projectbestand niet. `validate_registry_references`
(`references.py`, aan de haak `validate_project`) weigert daarom het opslaan, met de
componenten erbij die de registry nog gebruiken. Niet automatisch opruimen: dan verandert
stilletjes waar een image vandaan komt.

Voor de tweede route is daar een uitzondering voor nodig in
`_strip_removed_services_from_components` (`wizard_sections.py`, de `post_merge` van de
dienstensectie). Die hook draait VOOR `validate_project` en gooit elke componentvermelding weg
waarvan de dienst niet meer op projectniveau staat -- dan is de verwijzing er niet meer tegen
de tijd dat de grendel kijkt. Diensten die `component_selection_follows_config` declareren
slaat hij daarom over: daar IS de waarde de selectie, dus opruimen betekent stilletjes
veranderen waar een image vandaan komt. Zonder die uitzondering deed dezelfde handeling
bovendien twee verschillende dingen -- de lijstvorm op een component werd gestript, de
dict-vorm op een deployment-component niet, en die laatste werd dan wel geweigerd.

Dat staat naast `values_must_exist` en niet in plaats daarvan: die toets slaat een LEGE
keuzelijst met opzet over, en leeg is precies de toestand die hier ontstaat.

De afnemer schrijft altijd de UPSTREAM, nooit een adres van het platform. Dat houdt het
bestand overdraagbaar naar een ander platform.

### De keuze bij een component is een voorrangsregel, geen aan/uit

Wat een component aanvinkt bepaalt WELKE van je registries voorgaat, niet OF er een van je
registries geldt. `build_rules()` zet alle registries van het project in de lijst, met de
gekozen registry vooraan en de clustertabel erachter. Een component dat de dienst niet
aanvinkt maar wel een image draait die onder een van je eigen upstreams valt, gaat dus ook
langs je eigen proxy en krijgt dat pull-secret.

Bewust zo, en om twee redenen:

- Een registry-entry zegt "deze upstream is van mij, en dit is het token". Een image onder
  die prefix bij de GEDEELDE proxy laten uitkomen betekent een pull zonder credentials, en
  dus een `ImagePullBackOff` met de melding dat de image niet bestaat: precies de fout
  die de verkeerde kant op wijst.
- De manifestpas loopt over de hele deployment-map met dezelfde `build_rules()` en weet
  niet welk component welke container is. Zou de componentlus een andere lijst gebruiken,
  dan gaven de twee wegen een ander antwoord op dezelfde vraag, en herschreef de pas het
  daarna alsnog.

Het overwogen alternatief (voor een component met een keuze alleen die ene regel plus de
clustertabel, en zonder keuze alleen de clustertabel) is daarop afgewezen.

De afnemer hoort de consequentie wel te kennen, en `help.md` noemt haar: niet aanvinken is
geen keuze voor de publieke weg. Publiek is wat een image is als hij buiten al je eigen
registries valt. `TestEenEigenRegistryGeldtVoorHetHeleProject` in
`tests/test_image_registries_rules.py` pint het vast.

## Wat er niet in het bestand staat

De RCR-URL, de naam van de proxy-organisatie en de naam van het pull-secret staan er
bewust **niet** in. Ze zijn allebei een functie van de projectnaam en de upstream en worden
berekend op het moment dat een manifest wordt gegenereerd, net zoals databasenamen,
bucketnamen en keycloak client-ids dat al zijn. Niets opslaan betekent ook niets dat uit de
pas kan lopen met de werkelijkheid.

De naamregels staan in `opi/services/catalog/image_registries/naming.py`:

| Naam | Regel |
|---|---|
| `friendlyName` | de host zonder TLD en zonder punten (`code.overheid.nl` -> `codeoverheid`) |
| `suffix` | `<project>-<hash>`, met `<hash>` de eerste acht hex-tekens van sha256 over de hele upstream |
| organisatie | `<friendlyName>-<customerName>-<suffix>` |
| pull-secret | `<organisatie>-robot-pull-secret`, expliciet gezet want de operator laat de suffix weg |
| image-omzetting | host vervangen, namespace-segment eruit |

De upstream hoort in de suffix omdat `friendlyName` alleen de HOST draagt, en die zonder
TLD. Zonder dat deel komen `ghcr.io/orga` en `ghcr.io/orgb` van hetzelfde project op één
organisatienaam uit, en daarmee op één bestandsnaam op het projectniveau, één
credentials-secret en één bestemming, waarna de tweede registry de eerste stil overschrijft
en twee componenten die verschillende images bedoelen dezelfde ophalen.

Waarom een **hash** en niet de leesbare namespace: beide delen van de suffix gaan door
`sanitize_kubernetes_name`, en die maakt van `.` en `/` ook een koppelteken. Er blijft dus
geen teken over dat als grens kan dienen, en dan is de samenvoeging niet omkeerbaar.
Gemeten: project `demo` met upstream `ghcr.io/team` en project `demo-team` met upstream
`ghcr.io` kwamen allebei op suffix `demo-team` uit, dus op één tenantbrede organisatie, één
pull-secretnaam en één bestemming: twee projecten die elkaars proxy besturen. Binnen één
project deed hetzelfde zich voor tussen `.../robbert.uittenbroek`, `.../robbert/uittenbroek`
en `.../robbert-uittenbroek`. De hash loopt over de HELE upstream en niet alleen over het
pad, want `code.overheid.nl/x` en `code.overheid.com/x` leveren allebei `codeoverheid` op.

De naam is daarmee eenduidig, maar de naamREGEL is niet de enige grendel: bij het opslaan
weigert `validate_proxy_organization_claims` een organisatienaam die een ander project al
claimt (en twee entries van één project die op dezelfde naam uitkomen). Die toets meet de
uitkomst in plaats van de aanname, dus hij vangt ook afkapping op 63 tekens en een latere
naamwijziging.

## Eén regelvorm, twee bronnen

De clustertabel (de gedeelde proxy-caches die het platform aanbiedt) en een private registry
doen bijna hetzelfde, en dat is precies de reden om er niet twee mechanismen van te maken:

```
gedeeld:  match code.overheid.nl             -> to rcr.rijksapps.nl/code-overheid-rig
          code.overheid.nl/robbert/demo:tag  => rcr.rijksapps.nl/code-overheid-rig/robbert/demo:tag

privé:    match code.overheid.nl/robbert     -> to rcr.rijksapps.nl/codeoverheid-rig-<project>-robbert
          code.overheid.nl/robbert/demo:tag  => rcr.rijksapps.nl/codeoverheid-rig-<project>-robbert/demo:tag
```

Het namespace-segment dat bij de privévariant wegvalt is geen apart gedrag: het volgt uit
een langere match. Eén regelvorm dekt allebei (`rules.py`):

```python
@dataclass(frozen=True)
class RegistryRule:
    match: str    # upstream-prefix, met of zonder namespace
    to: str       # bestemming, host plus org
    secret: str   # het pull-secret dat bij die bestemming hoort
```

`resolve_image()` normaliseert de image, loopt de regels af en past de eerste match toe.
Projectregels staan vooraan, dus de eigen registry van een project wint van de gedeelde
proxy voor dezelfde upstream. Volgorde is data, geen code.

Drie dingen die in die ene functie horen en nergens anders:

1. **Normalisatie van korte namen.** `nginx:alpine` wordt eerst `docker.io/library/nginx:alpine`,
   anders matcht de `docker.io`-regel hem niet terwijl de admission-webhook van ODCN hem wel
   zo behandelt.
2. **Een image die al op de bestemming staat** wordt niet herschreven maar krijgt wel het
   secret van die regel (het dp-bn7-geval).
3. **Geen match** betekent image ongewijzigd en geen secret.

Een UPSTREAM-prefix gaat door een eigen normalisatie (`normalize_prefix()`), en dat is geen
dubbeling: een prefix is een pad en geen naam. `ghcr.io` is al compleet, terwijl
`normalize_image()` er `docker.io/library/ghcr.io` van maakt; die aanvulling hoort bij een
image-verwijzing (`nginx` is de naam van een repository) en niet bij een prefix. Wie de
image-normalisatie op een prefix loslaat, laat een upstream zonder pad (de vorm die
`algor-odc` in de vloot heeft) nooit meer matchen. De twee plekken die een prefix
vergelijken zijn de tokentoets (`enforcers.py`) en het vooruit invullen van het keuzeveld
(`providers.py`).

`display_image()` is de weg terug, voor de schermen: een gebruiker ziet de eigen registry
in plaats van de kale RCR-URL.

## Wat een afnemer plakt is zelden een upstream

`normalize_upstream()` (`upstream.py`) maakt van een geplakte browser-URL of een volledige
image-verwijzing de upstream die wij nodig hebben:

| Geplakt | Upstream |
|---|---|
| `https://code.overheid.nl/robbert/-/packages` | `code.overheid.nl/robbert` |
| `https://github.com/orgs/rijksictgilde/packages` | `ghcr.io/rijksictgilde` |
| `https://hub.docker.com/r/bitnami/nginx` | `docker.io/bitnami` |
| `https://gitlab.com/groep/project/container_registry` | `registry.gitlab.com/groep/project` |
| `code.overheid.nl/team/app:1.2` | `code.overheid.nl/team` |

Alleen een tag of digest onderscheidt een image-verwijzing van een upstream met een pad:
`code.overheid.nl/team` blijft dus zoals hij is. Een vorm die we niet kennen laten we met
rust op de generieke bewerkingen na (protocol eraf, kleine letters, geen afsluitende schuine
streep), zodat het patroon hem afwijst in plaats van er iets van te maken dat ergens anders
heen wijst. Een eigen GitLab wordt om die reden niet geraden: de registryhost is daar een
installatiekeuze.

`upstream_from_project_images()` doet het omgekeerde en vult het veld vooruit in: een
nieuwe registry krijgt de upstream die uit de images van dit project volgt, en alleen als
die eenduidig is. De afnemer heeft zijn image al ingetypt, en op ODCN kunnen we de vraag
niet weglaten -- daar wordt een proxy-organisatie aangemaakt voor precies een
upstream-namespace, en die moet er zijn voordat er een image is. Wijzen de images naar meer
dan een prefix, dan is de vraag juist het punt en vullen we niets in.

De omzetting hangt als `BeforeValidator` aan het veld in `config_model.py`, dus de API en het
formulier (via `ModelFieldValidator`, dat zijn toets uit de ANNOTATIE bouwt) krijgen hem
allebei; de schrijfkant van het formulier roept dezelfde functie aan via `UpstreamConverter`,
en de migratie 2.8 -> 2.9 ook.

Wat hij bewust NIET doet is een projectbestand repareren dat er al staat. De
hele-bestandspoort draait het model met `STORED_CONTEXT_KEY`, en daar slaat de omzetting
over: valideren schrijft niet terug, dus een opgeslagen `https://ghcr.io` zou door de poort
komen en ONgewijzigd in het bestand blijven staan, waarna `normalize_prefix` hem nooit matcht
en de registry stil niet meer geldt in plaats van luid geweigerd te worden.

De volgorde in `Annotated` is niet vrijblijvend: met het patroon VOOR de before-validator
staat het patroon ook in het gerenderde JSON-schema, en dat fragment is waar een client de
regel leest.

## De provisioning-backend

Het enige dat per cluster echt verschilt is wat er moet worden aangemaakt
(`backends.py`), en welke backend geldt staat in de clusterconfig
(`get_image_registries_config`):

| Backend | Wat `ensure` doet |
|---|---|
| `direct-secret` | een dockerconfigjson-secret in de namespace |
| `quay-proxy-organization` | een credentials-secret plus een `Organization` met proxyCache |

De groep en versie van de `Organization`-CRD staan in de clusterconfig
(`organization_api_version`) en niet in het sjabloon. Reden: het is een platformfeit dat per
cluster kan afwijken. Op `odcn-production` is het `quay.k8s.rijksapps.nl/v1alpha1`, gemeten
met `kubectl api-resources --api-group=quay.k8s.rijksapps.nl` en bevestigd door er op
2026-09-07 een `Organization` mee aan te maken die reconcileerde (`OrganizationReady`,
`ProxyCacheReady`). De waarde staat in de clusterconfig (`cluster_config.py`) en als terugval
in `backends.py`.

Neem hem niet over uit de operator-documentatie: die noemt `quay.redhat.com/v1`, en met die
waarde weigert de API-server elk gegenereerd manifest met `no matches for kind Organization in
version quay.redhat.com/v1` en blijft de projectapplicatie in ArgoCD hangen. Vraag het dus het
cluster. Wijkt hij af, dan is dat één regel clusterconfig in plaats van een sjabloonwijziging.

Een derde platform is een derde backend plus een tabel in de clusterconfig, en geen
wijziging aan de dienst.

## Het projectniveau in de deployments-repo

De deployments-repo heeft een laag boven de deployments gekregen:
`<cluster>/<project>/_project/`, met een eigen kustomization en een eigen ArgoCD-applicatie
op sync-wave 0 (de deployments staan op 1). Daar staat wat namespace-breed is in plaats van
van één deployment: vandaag het pull-secret en de proxy-organisatie.

De underscore is bewust: een deploymentnaam is een DNS-label en kan er geen bevatten, dus
deze map kan nooit botsen met een deployment. De applicatie heet `{project}-project`, en
daarom is `project` de eerste gereserveerde deploymentnaam
(`RESERVED_DEPLOYMENT_NAMES` in `opi/utils/naming.py`).

Wat er komt te staan bepalen de DIENSTEN, via de haak `contribute_project_manifests`,
dezelfde vorm als `contribute_deployment_manifests` een laag lager. De bestandsnaam begint
verplicht met de dienstnaam, zodat de symmetrische prune hem weer weghaalt zodra de dienst
uitgaat. De ACME-issuers, de tenant-baseline netwerkpolicy en de namespace zelf horen daar
ook thuis; die staan nog hardgecodeerd elders en kunnen later langs dezelfde haak.

Ordening en gereedheid zijn twee verschillende dingen. De sync-wave regelt alleen de
volgorde van aanmaken, niet of de proxy al klaar is. Dat hoeft ook niet strikt: een pod die
te vroeg is belandt in ImagePullBackOff en herstelt vanzelf zodra de proxy werkt.

### De serviceaccount is wél een harde volgorde

Voor de proxy geldt "te vroeg is niet erg", voor de SERVICEACCOUNT niet. Een Deployment die
naar een serviceaccount wijst die er nog niet is krijgt helemaal geen pod:

    Error creating: pods "productie-web-..." is forbidden: error looking up
    service account rig-waard-vqs/waard-vqs-sa: serviceaccount "waard-vqs-sa" not found

Gemeten op de sandbox op 2026-09-07. Het herstelt vanzelf, want de ReplicaSet probeert het
opnieuw en de OUDE pod blijft ondertussen draaien, dus er is geen storing, maar het kostte
2 minuten en 39 seconden nadat de serviceaccount er stond, want de ReplicaSet zit dan in zijn
FailedCreate-backoff.

De sync-wave lost dit niet op: die ordent RESOURCES binnen één applicatie, en het
projectniveau en een deployment zijn twee applicaties. Wat hem wel oplost staat in
`ProjectManager._process_application_manifests`: de projectapplicatie telt mee in de
bestaanscontrole die de umbrella-refresh aanzet, en er wordt op gewacht tot hij GESYNCT is
(niet alleen tot zijn CR bestaat) voordat de deployments uitrollen.

## Migratie (schemaversie 2.9)

- De root-`registries:` verhuist naar `services/image-registries/config/registries`, met
  `url` hernoemd naar `upstream`.
- **De waarde wordt daarbij omgezet, niet letterlijk gekopieerd.** Het oude
  `$defs/registry.url` liet een protocol expliciet toe en had geen hoofdletterregel;
  `UPSTREAM_PATTERN` verbiedt allebei, en ook de afsluitende schuine streep. De migratie
  haalt daarom `https://`, `http://`, `ssh://` of `git://` eraf, zet de waarde in kleine
  letters en knipt een afsluitende `/` weg. Zonder die omzetting
  migreert zo'n bestand wel (lezen valideert niet) maar sneuvelt het bij de eerste save, op
  een veld dat de gebruiker nooit heeft aangeraakt.
- De sleutel `registry:` op een deployment-component wordt een dienstvermelding op datzelfde
  component.
- Beide oude vormen blijven valideren onder hun eigen schemaversie via
  `opi/schemas/project_legacy/v2.8.json`.
- Het veld `registry:` binnen de config van `namespace-postgresql-database` blijft bestaan:
  dat is een verwijzing bij naam vanuit een andere dienst, en die wijst nu naar een entry in
  de config van `image-registries`.
- **De constraints verhuizen mee.** `$defs/registry` in `project_v2.json` droeg twee regels
  die er niet louter cosmetisch stonden, en die staan nu in `config_model.py`. Zie hieronder.

## De twee schemabewakers op de dienstconfig

Een sleutel die van de projectwortel naar een dienstconfig verhuist, verhuist van een schema
dat jsonschema draait naar een schema dat pydantic draait. De VORM reist dan mee, de
CONSTRAINTS niet vanzelf, en juist die twee waren hier dragend.

**`upstream` heeft een patroon** (`UPSTREAM_PATTERN`). Het manifest
`quay-proxy-organization.yaml.jinja` zet deze waarde in `spec.proxyCache.upstreamRegistry`.
Zonder patroon breekt een upstream met een aanhalingsteken en een regeleinde uit zijn
YAML-scalar, en dan staan er extra DOCUMENTEN in dat bestand: het gaat onversleuteld naar
`<cluster>/<project>/_project/`, komt in de kustomization en wordt door de projectapplicatie
gesynct, dus ArgoCD maakt ze aan als gewone namespaced resources. Twee onafhankelijke sloten:
het patroon weigert de waarde aan de poort, en het sjabloon quoteert hem met `yaml_scalar`
voor het geval hij er binnendoor toch komt (een bestaand bestand, een migratie).

Het patroon is strikter dan het oude `$defs/registry.url`: hostnaam met een punt (of een
poort, of `localhost`), eventueel een pad, kleine letters, geen protocol en geen tag of
digest. Allebei de waarden die de vloot werkelijk heeft (`ghcr.io` en `rcr.rijksapps.nl/rig`)
komen er nog door.

**`password` heeft het AGE-patroon** (`AGE_ENCRYPTED_OR_PLAIN_PATTERN`). Dat is niet alleen
een vormregel: `find_plaintext_secret_violations` herkent een AGE-veld AAN dat patroon, en
`ProjectStore._validate` draait die controle op ELKE schrijfroute, ook op de elf plekken
met `enforce_validation=False`, waar de rest van de validatie alleen wordt gelogd. De reden
staat er letterlijk bij: een teruggeschreven `get_decrypted()`-view zou anders
platte-tekst-credentials in git zetten, en `decrypt_tree()` ontsleutelt generiek elke
AGE-waarde in de boom, dus ook deze.

Die controle keek alleen naar `project_v2.json`, en een dienstconfig staat daar bewust niet
in. De helft die een dienst zelf beschrijft wordt daarom gemeten door
`find_plaintext_service_config_violations` (`opi/manager/project_validation.py`), die
dezelfde detectie op de configmodellen van de diensten draait. Beide helften worden in
`ProjectStore._validate` naast elkaar aangeroepen, en die AANHAKING heeft een eigen test die
via de store gaat (`test_de_store_weigert_het_token_ook_zonder_enforce`), zodat het opvalt
als iemand de tweede helft eruit haalt. Ook hier is de detectie AFGELEID van het
schema (een `pattern` met de AGE-markering) en niet van een handgeschreven veldenlijst, dus
een dienst die morgen een geheim gaat opslaan is gedekt zodra zijn model dat zegt.

### Drie opslagvormen, een lezer

Datzelfde patroon laat het token in drie vormen toe: het armored AGE-blok, de eenregelige
`base64+age:`-vorm en een expliciet als platte tekst gemarkeerde `plain:`-waarde. Dat is
geen randgeval maar de huisvorm van een projectbestand: de repository-password, de
api-key en de projectsleutel dragen allemaal de eenregelige vorm.

Wie de waarde uitleest moet ze dus alle drie kennen. Toetsen op alleen de armored markering
zet de andere twee LETTERLIJK in de `.dockerconfigjson` (of in het
`-upstream-credentials`-secret van Quay): geen fout, geen waarschuwing, wel een credential
dat niet klopt en een pod die op `invalid username/password` of `name unknown: repository
not found` blijft hangen. Beide lezers, `_plain_password` in `backends.py` en
`ProjectAgeSecretConverter` in `converters.py`, gaan daarom langs `carries_encrypted_value`
en `decrypt_password_smart_sync` (`opi/utils/age.py`), net als de CNPG-route dat met
`decrypt_password_smart` doet. In de formulierlaag is het verschil het scherpst: `read()`
zou anders de cijfertekst tonen en `write()` die als NIEUW token versleutelen, waarna het
echte token weg is na een opslag waarin niemand het veld aanraakte.

Is er wel een versleutelde waarde maar geen projectsleutel, dan stopt het schrijven met een
fout (`get_decoded_project_private_key_sync`). Stil geen secret schrijven levert een
deployment op die aan de pull blijft hangen zonder dat er iets in de weg stond.

### Vier van de vijf velden van de entry dragen een patroon

`upstream` en `password` waren de twee die met de sleutel mee hadden moeten verhuizen. Twee
van de drie andere velden van `RegistryEntry` hebben er om dezelfde reden een gekregen, want
beide komen ongequote in een gerenderd manifest terecht.

`name` is een DNS-1123-achtige naam die met een kleine LETTER begint
(`REGISTRY_NAME_PATTERN`), zodat hij nooit als YAML-getal wordt gelezen.

`secretName` is een RFC-1123-subdomeinnaam (`SECRET_NAME_PATTERN`, met `max_length=253`):
precies wat kubernetes een secret laat heten. Zonder patroon zette een waarde met
regeleindes er in `deployment.yaml.jinja` podvelden bij (`hostNetwork`, `hostPID`) en met
een `---` een TWEEDE document, tot een RoleBinding naar ClusterRole `cluster-admin` op de
`default` serviceaccount van de eigen namespace aan toe; de `AppProject` heeft
`namespaceResourceWhitelist` op group `*`, kind `*`, dus ArgoCD past dat toe.

Twee sloten, net als bij `upstream`. Het patroon weigert de waarde op de save-poorten en
aan de API-deur (`AddRegistryBySecretRequest`), en de twee sjablonen die de secretnaam
renderen quoteren hem met `yaml_scalar` voor het geval hij er binnendoor komt: de migratie
2.8 -> 2.9 (`relocate_registries_to_service`) en een bestaand projectbestand zetten
`secretName` ongetoetst over. Het tweede sjabloon is `postgresql-cluster.yaml.jinja`, waar
dezelfde naam terechtkomt via de sleutel `registry` in de config van
`namespace-postgresql-database`.

`validate_registry_entry_ownership` leest een `secretName` met
`removesuffix("-robot-pull-secret")` om de organisatie te vinden; RFC-1123 laat streepjes
toe, dus die vorm blijft heel.

Het vijfde veld, `username`, draagt bewust geen patroon: een echte gebruikersnaam mag een
punt, een apenstaartje of een hoofdletter bevatten en een vormregel zou die weigeren. Wat
hem tegenhoudt is het sjabloon. `generic-secret.yaml.to-sops.jinja` heeft twee takken: een
meerregelige waarde wordt een literal block met `indent(4, true)` en een eenregelige gaat
door `yaml_scalar`. Nagemeten met vijf payloads (een regeleinde met `kind: RoleBinding`
erachter, een `---`-blok met een RoleBinding naar `cluster-admin`, een quote-uitbraak met
gelijke inspringing, een kale CR, en leidende spaties) langs allebei de backends: de
quay-backend zet `username` rauw in `secret_pairs` en houdt één document met precies
`username` en `password`, en de direct-secret-backend laat de waarde eerst door
`json.dumps` van `RegistrySecret.to_dockerconfigjson`, die het regeleinde escapet voordat
`yaml_scalar` eraan komt.

## Een regel, een pad: formulier en API

De regel voor `upstream` staat in `config_model.py` en niet in het formulier. Dat model is
waar de API tegenaan schrijft en waar een opgeslagen projectbestand mee wordt gevalideerd;
het formulier hergebruikt dezelfde constraint via `ModelFieldValidator`, en levert alleen de
Nederlandse uitleg. Er is dus een definitie en geen tweeling die uit elkaar loopt.

Dat is niet theoretisch: de vorige vorm had de regel als `UpstreamValidator` in de
formulierlaag, en `POST /projects/{p}/registries/by-credentials` kwam daar niet langs. Die
twee endpoints (`by-secret` en `by-credentials`) schrijven nu tegen hetzelfde patroon, zodat
een aanroeper aan de deur wordt afgewezen in plaats van een laag dieper met een melding over
een schema.

## Wat we niet kunnen verbergen

En dus uitleggen in de helptekst (`help.md`): het token heeft leesrecht op packages nodig,
er zit een quotum op, het token roteert elke 90 dagen, en de dienst uitzetten verwijdert de
organisatie in RCR. Bovenstrooms verandert er niets: de images staan er nog.

## Bestanden

| Wat | Waar |
|---|---|
| Regelvorm en `resolve_image()` | `opi/services/catalog/image_registries/rules.py` |
| De twee bronnen samengevoegd | `opi/services/catalog/image_registries/resolution.py` |
| Naamregels en de slug uit een label | `opi/services/catalog/image_registries/naming.py` |
| Upstream uit wat er geplakt is | `opi/services/catalog/image_registries/upstream.py` |
| De weg terug bij het opslaan | `opi/services/catalog/image_registries/references.py` |
| Backends | `opi/services/catalog/image_registries/backends.py` |
| Eigendomsregels over het hele project | `opi/services/catalog/image_registries/ownership.py`, aan de haak `validate_project` |
| Clusterconfig | `get_image_registries_config` in `opi/core/cluster_config.py` |
| Organization-sjabloon | `manifests/quay-proxy-organization.yaml.jinja` |
| Projectniveau-haak | `contribute_project_manifests` in `opi/services/catalog/base.py` |
| Emitter en prune | `_process_project_manifests` in `opi/manager/project_manager.py` |
| ArgoCD-applicatie | `create_project_application` in `opi/manager/argo_manager.py` |

## De eigen serviceaccount per project

De ``default`` serviceaccount is geen vangnet dat we willen houden, om twee redenen. Hij
draagt elk pull-secret dat het platform in de namespace repliceert, dus ook dat van de
proxy-organisatie van een ander project: zolang onze pods daarop draaien is een private
registry alleen op papier privé. En technisch is het ook geen goed idee: alle secrets
wijzen naar dezelfde host en kubelet moet daar de juiste uit halen, wat bij negen secrets
al onzeker is en bij honderd een probleem.

Daarom draagt het projectniveau ook een eigen serviceaccount, `{project}-sa`, zonder
pull-secrets, bijgedragen door de **platform**-dienst (en niet door image-registries: elk
project heeft hem nodig, ook een project dat nooit een eigen registry opgeeft). Elke
gegenereerde podspec draagt de secrets die hij zelf nodig heeft, geleverd door
`resolve_image()`. Meestal is dat er precies één.

Gemeten op 2026-09-07 met een lege serviceaccount in `rig-prd-test`: een pod met
`alpine:3.20.3` faalt, dezelfde pod met het RCR-pad faalt, en dezelfde pod met een expliciet
secret in de podspec slaagt. De eerste rij is de belangrijkste: na admission stond er
`rcr.rijksapps.nl/dockerhub-rig/library/alpine:3.20.3`, inclusief het `library/`-segment.
Elke image belandt op een RCR-pad, elk RCR-pad vraagt authenticatie, en dus heeft élke pod
een secret nodig, ook voor een doodgewone publieke image.

Operators die hun eigen serviceaccount maken (CNPG met `rig-db`) draaien niet op de onze;
daar staat het secret in de resource-spec zelf.

### De overstap kost geen enkel recht

Gemeten in `rig-prd-test` op 2026-09-09. Naar de `default` serviceaccount wijst in de
namespace **geen enkele rolbinding**. Van de tien rolbindingen die er staan gaan er acht naar
platform-serviceaccounts uit andere namespaces (`argocd-server`,
`argocd-application-controller`, `external-dns`, `namespace-manager`) en twee naar de
OpenShift-standaarden `builder` en `deployer`. Een workload die overstapt naar `{project}-sa`
verliest dus niets: er was niets te verliezen.

Wat `default` wél draagt zijn acht `imagePullSecrets` plus capsule- en ODCN-labels, waaronder
`projectcapsule.dev/managed-by=replications`. Dat object is daarmee eigendom van de replicatie
van ODCN: haal je de pull-secrets er met de hand af, dan zet de replicatie ze terug. Een eigen
serviceaccount is dus niet de nette weg naast een andere, het is de enige weg.

Dat een kale serviceaccount genoeg is, is apart gemeten: op 2026-09-07 draaide een pod op een
serviceaccount zonder enige rolbinding en zonder pull-secret gewoon tot `Succeeded`, met alleen
een expliciet secret in de podspec. Alles wat een pod nodig heeft komt uit de namespace of uit
de podspec, niet uit de serviceaccount.

### Het label dat er nooit op mag

Onze serviceaccount mag **nooit** het label `customer.odc-noord.nl/replication=true` dragen.
Die sleutel komt uit de documentatie van ODC-Noord; wij hebben hem **niet** zelf gemeten en
weten dus niet zeker dat de replicatie erop selecteert. Wat we wel hebben gezien wijst twee
andere kanten op: van de meting in `rig-prd-test` hierboven is als ODCN-label
`projectcapsule.dev/managed-by=replications` opgeschreven en niet deze, en de proef van
2026-09-07 beschrijft het gedrag als "hangt het aan elke `default` serviceaccount", dus op
naam. Welke van de drie het is (dit label, dat label, of de naam) staat als vraag 7 bij
"Vragen aan ODC-Noord" in `plans/private-images-uit-een-eigen-registry.md`.

Voor ons gedrag maakt het niets uit: onze serviceaccount heet `{project}-sa` en draagt alleen
`app.kubernetes.io/name`, `app.kubernetes.io/component` en `created-by`, dus geen van beide
labels. `manifests/project-serviceaccount.yaml.jinja` zet het label niet, en dat blijft zo:
`test_project_service_account.py` toetst dat het er niet op staat. Zolang de selector niet
vaststaat is dat verbod goedkope voorzorg en geen gemeten noodzaak.

## Validaties

| Wat | Waar | Waarom |
|---|---|---|
| Het token wordt bij het opslaan getoetst | `enforcers.RegistryTokenEnforcer` | Een te smal token komt anders pas naar boven als `ImagePullBackOff` met de melding `repository not found`, en die wijst de verkeerde kant op |
| Een verwijzing naar de proxy-organisatie van een ander project | `validate_proxy_organization_ownership` | Wie de naam van andermans organisatie kent leest er met ANDERMANS credentials uit |
| Een registry-ENTRY die naar de organisatie van een ander project wijst (`upstream` of `secretName`) | `validate_registry_entry_ownership` | De entry maakt een regel die op elke image onder die upstream slaat en het opgegeven secret eraan hangt, ook op routes zonder projectbestand |
| Een organisatienaam die een ander project al claimt, of twee entries van één project op één naam | `validate_proxy_organization_claims` | De organisatienaam is tenantbreed: twee CR's met dezelfde naam sturen één organisatie in RCR aan |
| De ingetypte image van een ad-hoc job | `foreign_proxy_organization_owner` in `JobManager.begin` | Die image komt uit een formulierveld en ziet geen enkele validator bij het opslaan, terwijl `apply_bundle` er wel de projectregels op toepast |
| Een registrynaam die niet bestaat | `values_must_exist` op de componentkeuze | Een typefout hoort bij het opslaan te sneuvelen, niet pas bij het pullen |
| `project` als deploymentnaam | `RESERVED_DEPLOYMENT_NAMES` | De ArgoCD-applicatie van het projectniveau heet `{project}-project` |

De drie eigendomsregels staan in `ownership.py` bij de dienst en niet in de gedeelde
validator: ze kijken naar de andere projecten op het cluster en niet naar één configblok,
dus `validate_config` kan ze niet beoordelen. Ze hangen aan `Service.validate_project`, de
haak die `validate_project_structure` voor elke dienst afloopt; dat is de poort waar elke
schrijfroute langskomt. De haak draait op ELK project, ook op een dat de dienst niet
aanvinkt: het gaat erom waar een image naar wijst, en dat zou anders te omzeilen zijn door
de dienst weg te laten.

De tokentoets meet wat er te meten valt: het tag-overzicht (`list-tags`, dus de
`tags/list`-aanroep die het leesrecht nodig heeft) van een repository waar dit project
werkelijk een image uit haalt. Hij leest de samengevoegde data van de flow
(`WizardState.get_merged_data`). In de edit-wizard en de modals staan de bestaande
componenten daar altijd in; alleen bij de eerste stap vooruit in de create-wizard, waar de
registry vóór de componenten komt, nog niet. Is er geen image onder de upstream, dan wordt er
niets geweigerd: een weigering op iets wat we niet gemeten hebben blokkeert een gebruiker op
een aanname. Bij de eindinzending draait `_validate_whole_flow` de toets opnieuw over alle
secties, en dan staan de componenten er in de create-wizard ook bij.

De toets draait NA de formulierverwerking, en die heeft het token op dat moment al
versleuteld. Hij pakt de opgeslagen waarde dus eerst uit, alle drie de opslagvormen die
het veld mag dragen, net als de secretbouwer in `backends.py`, voordat hij hem aan
skopeo geeft. Zonder dat uitpakken toetst hij het cijfertekstblok en wordt een geldig
token geweigerd. Is de waarde niet uit te pakken (geen sleutel), dan wordt er niets
getoetst en dus niets geweigerd.

## Op de projectpagina

De dienst levert een blok met wat de afnemer heeft ingevuld, en haalt de toestand van de
proxy er met een htmx-lazyload bij (`web.py`): `proxyCache.ready`,
`credentialsConfigured` en de verloopdatum van het token. Dat staat namelijk niet in het
projectbestand maar in het cluster, en een blok dat rendert mag geen connector aanroepen.

Een organisatie die er nog niet is, is geen fout: de proef mat 20 tot 25 seconden. Het blok
laat een wachtende afnemer zien wáárom er gewacht wordt.

Het statusendpoint is een eigen route en draagt dus zijn eigen eigendomscontrole: wie geen
lid van het project is krijgt 403, dezelfde vorm als bij de backups.
