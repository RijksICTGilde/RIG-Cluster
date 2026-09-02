# Het ZAD-projectbestand: bestaat het totaalplaatje al?

Datum: 2026-09-02. Aanleiding: de vraag of het idee van één declaratief projectbestand (image, services, poorten, afhankelijkheden, Keycloak-inrichting, versleutelde secrets en user env vars) al elders bestaat, en hoe we ZAD daartegen positioneren.

Korte conclusie: elk afzonderlijk stuk bestaat, en van bijna elk veld in ons schema is er prior art. De combinatie van workload, data, netwerk, identiteit en secrets in één bestand dat het team zelf bewerkt, tegen een zelfgehoste Keycloak, kom ik nergens compleet tegen. Twee projecten komen dichtbij: Otomi en NAIS.

## 1. Compose-achtige PaaS-manifests

Dit is de familie waar het projectbestand het meest op lijkt. De Compose Spec zelf doet image, ports, `depends_on`, volumes en env, en stopt daar. De PaaS-varianten gaan verder:

- [Render Blueprint](https://render.com/docs/blueprint-spec) (`render.yaml`): services, databases, `envVarGroups`, en `generateValue: true` voor een random 256-bits secret bij de eerste deploy. Dat is functioneel gelijk aan onze `@secret-gen:random:XX`.
- [Heroku `app.json`](https://devcenter.heroku.com/articles/app-json-schema): addons als afhankelijkheden, plus `env` met `generator: secret` (64 hex-tekens).
- DigitalOcean App Spec: componenten, routes, databases, en env vars met `type: SECRET` die versleuteld worden opgeslagen.

Wat in deze hele familie ontbreekt is IAM. Geen van deze formaten kent het begrip rol, client of scope; identiteit is altijd iets dat je ernaast regelt.

## 2. Workload-specs uit platform engineering

- [Score](https://docs.score.dev/docs/) (CNCF Sandbox): platform-agnostische workloadspec met containers en `resources` als abstracte afhankelijkheden (`type: postgres`), waarbij het platform de binding invult. Bewust arm gehouden: geen IAM, geen secretsopslag.
- [KubeVela / OAM](https://kubevela.io/docs/getting-started/core-concept/): components, traits, policies en workflow, uitbreidbaar met eigen definities. Je kunt er een eigen `keycloak-client`-trait in bouwen, maar out of the box zit het er niet in.
- [Radius](https://github.com/radius-project/radius) (Microsoft): modelleert de hele applicatie inclusief afhankelijkheden en connecties als één zelfstandige definitie, met een application graph. Sterkste totaalplaatje qua modelleren, maar identiteit is er geen eersterangs concept.

Achtergrond: [Score over het verschil met OAM en KubeVela](https://score.dev/blog/score-vs-open-application-model-kubevela/) en de [CNCF-analyse van workloadspecificaties](https://www.cncf.io/blog/2023/11/13/decoding-workload-specification-for-effective-platform-engineering/).

## 3. Orchestrators die IAM wel kunnen, maar niet in het developer-bestand

[Humanitec](https://developer.humanitec.com/app-humanitec-io/docs/humanitec-vs-others/kratix-etc./) (Score plus Resource Definitions, graph-based) en [Kratix](https://docs.kratix.io/) (Promises als contract tussen platform en team) splitsen bewust: de developer schrijft een dun bestand, het platform bepaalt wat een `postgres` of een `auth` betekent.

Voor Keycloak bestaat [crossplane-contrib/provider-keycloak](https://github.com/crossplane-contrib/provider-keycloak), gegenereerd uit de Terraform-provider, met CRD's voor realms, clients, rollen, service-account-rollen en protocol mappers. Realm-configuratie als code is dus volledig opgelost terrein. Het zit alleen in de platformlaag, niet in het projectbestand van het team.

## 4. Het dichtst bij ZAD: Otomi en NAIS

[Otomi, tegenwoordig Akamai App Platform](https://techdocs.akamai.com/cloud-computing/docs/application-platform): kant-en-klare PaaS op Kubernetes, met teams-onboarding, [Keycloak voor volledige RBAC per team](https://srodenhuis.medium.com/keycloak-integrated-into-otomi-container-platform-9ebbaafffcf6), een self-service console voor exposen van services, CNAME's, netwerkbeleid en secrets, de desired state in een git values-repo, en [SOPS met AGE](https://techdocs.akamai.com/app-platform/docs/manage-age) voor versleuteling. Dat is onze stack, tot en met de sleutelkeuze. Het verschil: de eenheid is een *team* met een values-boom, niet één projectbestand dat een applicatie plus haar rollen beschrijft.

[NAIS](https://doc.nais.io/workloads/application/reference/application-spec/) van NAV (Noorse overheid) is inhoudelijk het dichtst bij onze ambitie. Eén `app.yaml` bevat:

- image, command, ports
- `env` en `envFrom` (ConfigMaps, Secrets), `filesFrom` voor volumes
- `ingresses[]` met gewenste hostnames
- `accessPolicy` voor zero-trust netwerkbeleid, in- en uitgaand, inclusief externe hosts
- een `gcp`-blok dat Cloud SQL, BigQuery, buckets en IAM-rollen provisioneert
- een `azure`-blok dat een Entra ID-applicatie aanmaakt inclusief groepen, scopes en rollen, met een sidecar die de OIDC-flow afhandelt
- daarnaast ID-porten, Maskinporten en TokenX

Dat is precies "en ook de inrichting voor het identiteitssysteem, rollen en rechten", alleen tegen Entra ID en GCP in plaats van tegen Keycloak. Zie ook het [voorbeeldmanifest](https://doc.nais.io/workloads/application/reference/application-example/).

## 5. Vergelijkingsmatrix

Assen: (a) image en poorten, (b) afhankelijkheden zoals database, storage, queue, (c) IAM: rollen, clients en rechten, (d) secrets versleuteld in git, (e) user env vars met generatoren, (f) git als bron van waarheid, (g) één bestand dat het team zelf bewerkt.

| Oplossing | a | b | c | d | e | f | g |
|---|---|---|---|---|---|---|---|
| Compose Spec | ja | deels | nee | nee | deels | n.v.t. | ja |
| Render Blueprint | ja | ja | nee | nee | ja | ja | ja |
| Heroku app.json | ja | ja (addons) | nee | nee | ja | ja | ja |
| DigitalOcean App Spec | ja | ja | nee | platform | ja | ja | ja |
| Score | ja | abstract | nee | nee | deels | ja | ja |
| KubeVela / OAM | ja | via traits | uitbreidbaar | nee | deels | ja | ja |
| Radius | ja | ja | nee | nee | deels | ja | ja |
| Humanitec | ja | ja | platformlaag | nee | ja | deels | ja |
| Kratix | ja | ja | platformlaag | nee | deels | ja | ja |
| Crossplane + provider-keycloak | los | ja | ja | nee | nee | ja | nee |
| Otomi / Akamai App Platform | ja | ja | ja (per team) | ja (SOPS+AGE) | ja | ja | deels |
| NAIS | ja | ja | ja (Entra ID) | nee (Secret Manager) | ja | ja | ja |
| **ZAD** | ja | ja | ja (Keycloak) | ja (AGE in git) | ja | ja | ja |

## 6. Wat dan wel nieuw is aan ZAD

Niet het idee van een applicatiebestand, en niet elk afzonderlijk veld. Wat nergens compleet terugkomt is deze combinatie:

1. IdP-inrichting (rollen, rechten, clients) als eersterangs veld in hetzelfde bestand dat de developer zelf bewerkt, tegen een zelfgehoste Keycloak in plaats van een cloud-IdP. NAIS heeft de vorm maar hangt aan Entra ID; Otomi heeft Keycloak maar op teamniveau.
2. Versleutelde user env vars in datzelfde bestand, in git, met AGE, plus generatoren. Render en Heroku hebben generatoren maar geen versleutelde opslag in je eigen repo; Otomi heeft SOPS maar in een aparte values-repo.
3. Eén bestand als enige bron voor drie afgeleide repositories, waarbij het projectbestand het contract is en de rest gegenereerd blijft.

Daar komt de soevereiniteitsgrond bij: geen Entra ID, geen SaaS-orchestrator, geen afhankelijkheid van een cloudleverancier voor identiteit of secrets.

## 7. Twee kanttekeningen

Het risico dat elk van deze formaten treft is dat het projectbestand langzaam een lekkende superset van Kubernetes wordt. Score verdedigt zich daartegen door bewust arm te blijven; NAIS doet het tegenovergestelde en heeft een spec van honderden velden. Wij zitten op het NAIS-pad. Dat is een keuze en geen ongeluk, maar het is de moeite waard om dat expliciet te maken en er een grens bij te benoemen.

En het argument "wij hebben een projectbestand" is op zichzelf zwak, want dat heeft iedereen. Het sterke argument is: één bestand dekt workload, data, netwerk, identiteit en secrets, en het is de enige plek waar een team iets hoeft te bewerken. Dat kun je bij Score, Radius en Compose niet zeggen.

## Bronnen

- [Score docs](https://docs.score.dev/docs/)
- [Score vs OAM en KubeVela](https://score.dev/blog/score-vs-open-application-model-kubevela/)
- [CNCF: decoding workload specification for effective platform engineering](https://www.cncf.io/blog/2023/11/13/decoding-workload-specification-for-effective-platform-engineering/)
- [Radius](https://github.com/radius-project/radius)
- [KubeVela core concepts](https://kubevela.io/docs/getting-started/core-concept/)
- [Kratix docs](https://docs.kratix.io/)
- [Humanitec vs Kratix en anderen](https://developer.humanitec.com/app-humanitec-io/docs/humanitec-vs-others/kratix-etc./)
- [crossplane-contrib/provider-keycloak](https://github.com/crossplane-contrib/provider-keycloak)
- [Akamai App Platform (Otomi)](https://techdocs.akamai.com/cloud-computing/docs/application-platform)
- [Otomi: AGE-sleutelbeheer](https://techdocs.akamai.com/app-platform/docs/manage-age)
- [Keycloak in Otomi](https://srodenhuis.medium.com/keycloak-integrated-into-otomi-container-platform-9ebbaafffcf6)
- [NAIS Application spec](https://doc.nais.io/workloads/application/reference/application-spec/)
- [NAIS voorbeeldmanifest](https://doc.nais.io/workloads/application/reference/application-example/)
- [Render Blueprint spec](https://render.com/docs/blueprint-spec)
- [Heroku app.json schema](https://devcenter.heroku.com/articles/app-json-schema)
