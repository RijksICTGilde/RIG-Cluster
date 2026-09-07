# Eigen container registries (`image-registries`)

Een afnemer die een image draait uit zijn eigen private registry vult in wat hij van
zichzelf weet: de registry en een token. Wat er technisch onder gebeurt verschilt per
cluster, en dat verschil hoort hij niet te merken.

```
afnemer geeft:  upstream + gebruikersnaam + token          (eenmalig, projectniveau)
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

De afnemer schrijft altijd de UPSTREAM, nooit een adres van het platform. Dat houdt het
bestand overdraagbaar naar een ander platform.

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
| `suffix` | `<project>-<upstream-namespace>`, of alleen `<project>` als de upstream geen pad heeft |
| organisatie | `<friendlyName>-<customerName>-<suffix>` |
| pull-secret | `<organisatie>-robot-pull-secret`, expliciet gezet want de operator laat de suffix weg |
| image-omzetting | host vervangen, namespace-segment eruit |

De upstream-namespace hoort in de suffix omdat `friendlyName` alleen de HOST draagt. Zonder
dat deel komen `ghcr.io/orga` en `ghcr.io/orgb` van hetzelfde project op één
organisatienaam uit -- en daarmee op één bestandsnaam op het projectniveau, één
credentials-secret en één bestemming, waarna de tweede registry de eerste stil overschrijft
en twee componenten die verschillende images bedoelen dezelfde ophalen.

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

`display_image()` is de weg terug, voor de schermen: een gebruiker ziet zijn eigen registry
in plaats van de kale RCR-URL.

## De provisioning-backend

Het enige dat per cluster echt verschilt is wat er moet worden aangemaakt
(`backends.py`), en welke backend geldt staat in de clusterconfig
(`get_image_registries_config`):

| Backend | Wat `ensure` doet |
|---|---|
| `direct-secret` | een dockerconfigjson-secret in de namespace |
| `quay-proxy-organization` | een credentials-secret plus een `Organization` met proxyCache |

De groep en versie van de `Organization`-CRD staan in de clusterconfig
(`organization_api_version`) en niet in het sjabloon. Reden: het is een platformfeit van
ODC-Noord, de proef op productie is met de hand gedaan en de exacte apiVersion is niet in
dit repo vastgelegd. Wijkt hij af, dan is dat één regel clusterconfig in plaats van een
sjabloonwijziging.

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

Wat er komt te staan bepalen de DIENSTEN, via de haak `contribute_project_manifests` --
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

Gemeten op de sandbox op 2026-09-07. Het herstelt vanzelf -- de ReplicaSet probeert het
opnieuw en de OUDE pod blijft ondertussen draaien, dus er is geen storing -- maar het kostte
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
- De sleutel `registry:` op een deployment-component wordt een dienstvermelding op datzelfde
  component.
- Beide oude vormen blijven valideren onder hun eigen schemaversie via
  `opi/schemas/project_legacy/v2.8.json`.
- Het veld `registry:` binnen de config van `namespace-postgresql-database` blijft bestaan:
  dat is een verwijzing bij naam vanuit een andere dienst, en die wijst nu naar een entry in
  de config van `image-registries`.

## Wat we niet kunnen verbergen

En dus uitleggen in de helptekst (`help.md`): het token heeft leesrecht op packages nodig,
er zit een quotum op, het token roteert elke 90 dagen, en de dienst uitzetten verwijdert de
organisatie in RCR. Bovenstrooms verandert er niets: de images staan er nog.

## Bestanden

| Wat | Waar |
|---|---|
| Regelvorm en `resolve_image()` | `opi/services/catalog/image_registries/rules.py` |
| De twee bronnen samengevoegd | `opi/services/catalog/image_registries/resolution.py` |
| Naamregels | `opi/services/catalog/image_registries/naming.py` |
| Backends | `opi/services/catalog/image_registries/backends.py` |
| Clusterconfig | `get_image_registries_config` in `opi/core/cluster_config.py` |
| Organization-sjabloon | `manifests/quay-proxy-organization.yaml.jinja` |
| Projectniveau-haak | `contribute_project_manifests` in `opi/services/catalog/base.py` |
| Emitter en prune | `_process_project_manifests` in `opi/manager/project_manager.py` |
| ArgoCD-applicatie | `create_project_application` in `opi/manager/argo_manager.py` |

## De eigen serviceaccount per project

De ``default`` serviceaccount is geen vangnet dat we willen houden, om twee redenen. Hij
draagt elk pull-secret dat het platform in de namespace repliceert, dus ook dat van de
proxy-organisatie van een ander project: zolang onze pods daarop draaien is een private
registry alleen op papier privé. En technisch is het ook geen goed idee -- alle secrets
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
een secret nodig -- ook voor een doodgewone publieke image.

Operators die hun eigen serviceaccount maken (CNPG met `rig-db`) draaien niet op de onze;
daar staat het secret in de resource-spec zelf.

## Validaties

| Wat | Waar | Waarom |
|---|---|---|
| Het token wordt bij het opslaan getoetst | `enforcers.RegistryTokenEnforcer` | Een te smal token komt anders pas naar boven als `ImagePullBackOff` met de melding `repository not found`, en die wijst de verkeerde kant op |
| Een verwijzing naar de proxy-organisatie van een ander project | `validate_proxy_organization_ownership` | Wie de naam van andermans organisatie kent leest er met ANDERMANS credentials uit |
| Een registrynaam die niet bestaat | `values_must_exist` op de componentkeuze | Een typefout hoort bij het opslaan te sneuvelen, niet pas bij het pullen |
| `project` als deploymentnaam | `RESERVED_DEPLOYMENT_NAMES` | De ArgoCD-applicatie van het projectniveau heet `{project}-project` |

De tokentoets meet wat er te meten valt: het tag-overzicht (`list-tags`, dus de
`tags/list`-aanroep die het leesrecht nodig heeft) van een repository waar dit project
werkelijk een image uit haalt. Is er nog geen zo'n image -- de normale toestand in de
wizard, waar de registry vóór de componenten komt -- dan wordt er niets geweigerd: een
weigering op iets wat we niet gemeten hebben blokkeert een gebruiker op een aanname.

## Op de projectpagina

De dienst levert een blok met wat de afnemer heeft ingevuld, en haalt de toestand van de
proxy er met een htmx-lazyload bij (`web.py`): `proxyCache.ready`,
`credentialsConfigured` en de verloopdatum van het token. Dat staat namelijk niet in het
projectbestand maar in het cluster, en een blok dat rendert mag geen connector aanroepen.

Een organisatie die er nog niet is, is geen fout: de proef mat 20 tot 25 seconden. Het blok
is er zodat een wachtende afnemer ziet wáárom hij wacht.
