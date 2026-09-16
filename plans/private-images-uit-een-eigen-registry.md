# Private images uit een eigen registry

**Status**: ontwerp, nog niets gebouwd. De proef op productie is gedaan en volledig opgeruimd op 2026-09-07.
**Datum**: 2026-09-07
**Context**: `features/private-container-registries.md` (het bestaande `registries:`-blok), `features/futures/registry-proxy.md` (eerder ontwerp voor een RegistryProxy-CRD), `features/futures/platform-registry-pull-secret-wiring.md` (de dp-bn7-storing), `features/futures/wizard-registry-configuration.md` (eerder UI-voorstel), `instructions/services.md` (hoe een dienst in elkaar zit)

## Wat we bouwen

Een afnemer wil een image draaien die in zijn eigen private registry staat, bijvoorbeeld `code.overheid.nl/robbert.uittenbroek/zad-deployment-demo:0a611d9d`. Hij vult in wat hij van zichzelf weet: de registry en een token. Wat er technisch onder gebeurt verschilt per platform, en dat verschil hoort hij niet te merken.

```
afnemer geeft:  upstream + gebruikersnaam + token          (eenmalig, projectniveau)
                per component: welke registry hoort bij deze image

ZAD doet op een cluster met internettoegang:
                dockerconfigjson-secret in de namespace, image blijft ongewijzigd

ZAD doet op een cluster achter een Quay-operator (ODCN, en elk toekomstig cluster met dezelfde operator):
                Organization met proxyCache + credentials, wachten op ready,
                image herschrijven naar rcr.rijksapps.nl/<org>/<repo>:<tag>
```

Het projectbestand is op beide platforms identiek. Alleen de provisioning verschilt.

## Wat er vandaag al is

- `registries:` bestaat als top-level blok in het schema (`opi/schemas/project_v2.json`, `$defs.registry`): `name`, `url`, en dan of `username` plus AGE-`password`, of `secretName`.
- `registry:` bestaat op `deployments[].components[]`, niet op de componentdefinitie.
- `project_manager.py:5557` bouwt per deployment een `imagePullSecretsMap` en maakt bij credentials een `RegistrySecret` (`opi/utils/secrets.py:420`). `manifests/deployment.yaml.jinja:51` zet er één `imagePullSecrets` op.
- `upsert_registry_by_secret()` (`opi/manager/project_manager.py:8744`) schrijft idempotent een entry, aangeroepen vanuit `POST /projects/{p}/registries/by-secret`.
- `RegistryRewriteExtension` (`opi/extensions/registry_rewrite.py`) herschrijft images naar de gedeelde proxy-caches, met de mappings per cluster in `extensions/odcn-registry-rewrite.yaml` en `cluster_config.py:268`.
- Er is geen enkele UI: in `opi/forms/` komt `registries` nergens voor.

Gebruik in de vloot: 2 van de 49 projectbestanden hebben een `registries:`-blok. `algor-odc.yaml` (ghcr.io met credentials, verwezen vanaf twee deployment-componenten en vanuit `namespace-postgresql-database`) en `dp-bn7.yaml` (`secretName: rig-robot-pull-secret`, het noodverband uit de storing van mei).

## Wat de proef heeft aangetoond

Gemeten op `odcn-production` op 2026-09-07 met twee wegwerp-organisaties, daarna verwijderd.

| Vraag | Uitkomst |
|---|---|
| Kan een proxy op één namespace van een upstream? | Ja. `upstreamRegistry: code.overheid.nl/robbert.uittenbroek` werkt, met eigen token per org. |
| Hoe ziet de image-URL er dan uit? | `rcr.rijksapps.nl/codeoverheid-rig-demo/zad-deployment-demo:<tag>`, dus zonder het namespace-segment. |
| Hoe snel is de organisatie klaar? | 20 tot 25 seconden. `status.proxyCache.ready` en `credentialsConfigured` zijn bruikbaar als poort. |
| Kunnen credentials vervangen worden? | Ja. Secret bijwerken, binnen 40 seconden verandert `credentialsHash` en synct de operator. Alleen van geen-credentials naar wel vraagt volgens Red Hat een nieuwe proxy cache. |
| Hoe heet het pull-secret? | `<friendlyName>-<customerName>-robot-pull-secret`, dus **zonder** de suffix. Twee projecten met dezelfde upstream botsen. `robot.imagePullSecret.name` moeten we zelf zetten. |
| Wat doet `rotation.enabled: false`? | Niet wat de documentatie belooft. Het token krijgt alsnog `retentionDays: 90` en verloopt, zonder dat iemand het ververst. Altijd `rotation.enabled: true`. |
| Blijft het pull-secret in de eigen namespace? | Nee. ODCN's `GlobalTenantResource pullsecrets-rig` repliceert het in 7 tot 10 minuten naar alle `rig-prd-*` namespaces en hangt het aan elke `default` serviceaccount. Niets in de CR houdt dat tegen; `imagePullSecret.enabled: false` levert helemaal geen credential op. |
| Ruimt verwijderen netjes op? | Ja. CR weg is bronsecret weg (ownerReference met `blockOwnerDeletion`), replica's binnen anderhalve minuut, verwijzingen op de serviceaccounts na vijf minuten. |
| Wat als het token te weinig mag? | Upstream antwoordt `reqPackageAccess` (401), Quay vertaalt dat naar `name unknown: repository not found` en de afnemer ziet `ImagePullBackOff` met een melding die de verkeerde kant op wijst. |
| Cache na privé worden | De gedeelde `code-overheid-rig` heeft de image nog uit de tijd dat het pakket publiek was. Een upstream privé maken haalt niets uit de cache. |

## Het contract met de afnemer

Hij vult drie dingen in: de image die hij wil draaien, een gebruikersnaam en een token. Al het andere leiden wij af.

| Veld | Herkomst |
|---|---|
| `upstreamRegistry` | uit de image: host plus eerste padsegment, zichtbaar getoond en bij te stellen |
| `friendlyName` | uit de host, punten eruit (`code.overheid.nl` wordt `codeoverheid`) |
| `suffix` | de projectnaam |
| `customerName`, `tenants`, `quota`, rotatie | clusterconfig |
| naam van het pull-secret | berekend uit de projectnaam, want de operator laat de suffix weg |
| de RCR-URL en de uiteindelijke image-URL | berekend bij het genereren van het manifest, niet opgeslagen |

Wat we niet kunnen verbergen en dus uitleggen in helptekst en op de detailpagina: het token heeft leesrecht op packages nodig, er zit een quotum op, het token roteert elke 90 dagen, en de dienst uitzetten verwijdert de organisatie in RCR.

## Vorm in het projectbestand

De afnemer vult in de dienstconfig één of meer registries in. Naam, upstream inclusief pad, gebruikersnaam en token. Meer staat er niet in, en er wordt ook niets bij teruggeschreven.

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

De RCR-URL en de naam van het pull-secret staan er bewust **niet** in. Ze zijn allebei een functie van de projectnaam en de upstream, dus ze worden berekend op het moment dat een manifest wordt gegenereerd, net zoals databasenamen, bucketnamen en keycloak client-ids dat al zijn. Niets opslaan betekent ook niets dat uit de pas kan lopen met de werkelijkheid, geen terugschrijflus die op provisioning moet wachten, en geen extra herversleuteling van het projectbestand.

Bij een component staat alleen een verwijzing bij naam, en alleen als er iets te verwijzen valt:

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

"Publieke registry" is een non-waarde: dan staat de dienst niet bij de component en is er dus ook geen configblok. Afwezig betekent publiek, en dat is wat er vandaag al bij elke component zonder registryverwijzing gebeurt. Het scheelt een sleutel die in vrijwel elk project niets zegt, en het maakt het verschil tussen "hier is over nagedacht" en "hier hoefde niet over nagedacht te worden" onzichtbaar in het bestand, wat klopt.

De deployment mag de keuze overschrijven met dezelfde dienstvermelding onder `deployments[].components[].services`, dezelfde vorm die `publish-on-web` en `temp-storage` daar vandaag ook gebruiken.

De afnemer schrijft de upstream-URL, nooit een RCR-URL. Dat houdt het bestand overdraagbaar naar een ander platform en het is precies wat er vandaag met de publieke proxies ook gebeurt.

### Het keuzeveld komt van de dienst, niet uit het componentformulier

De verwijzing is componentconfig van de dienst, geen losse sleutel op de component. Dus geen `registry:` naast `image:`, maar een gewone dienstvermelding met een configblok, in dezelfde vorm die `publish-on-web` en `temp-storage` al gebruiken. Daarmee is dit precies het geval dat `instructions/services.md` "volledig automatisch" noemt: drie haken en klaar, `config_editables(ConfigLayer.COMPONENT)` voor het pad en de validatie, `config_component_visualizers()` voor de widget en `config_component_layout()` voor de plek in het formulier. Geen `owned_property`, geen uitbreiding aan het formulierraamwerk, en geen hardgecodeerd veld in het componentformulier.

De afnemer vinkt de dienst aan bij de component en kiest dan welke registry. Vinkt hij niets aan, dan is de image publiek en staat er niets in het bestand. Dat is de non-waarde: geen sleutel, geen leeg configblok.

De opties komen van een options-provider die de registries uit de dienstconfig van dít project leest, met `values_must_exist` erop, zodat een verwijzing naar een registry die niet bestaat bij het opslaan sneuvelt in plaats van pas bij het pullen. Wij vullen de keuze vooruit in zodra de image-prefix bij precies één registry past, dus in het gewone geval bevestigt de afnemer alleen.

De bestaande sleutel `registry:` op `deployments[].components[]` verdwijnt uit het schema. Er zijn drie verwijzingen in de vloot (twee in `algor-odc`, één in `dp-bn7`) en die migreren mee. Het veld `registry:` binnen de config van `namespace-postgresql-database` blijft wel bestaan: dat is een verwijzing bij naam vanuit een andere dienst en die blijft werken, alleen wijst hij straks naar een entry in de config van `image-registries`.

## Eén dienst: `image-registries`

Naamvoorstel, en bewust naar het onderwerp genoemd in plaats van naar de functie. Niet "private registries", want de dienst regelt ook wat er met publieke images gebeurt, en niet "repositories", want Quay gebruikt dat woord voor de dingen ín een registry.

De dienst kent twee bronnen van waarheid en één bewerking.

**Clusterconfiguratie**, niet door een gebruiker te bewerken: welke provisioning-backend hier geldt, de platformfeiten (`customerName`, `tenants`, quota, de RCR-host) en de tabel van upstream naar gedeelde proxy inclusief het secret dat erbij hoort. Dat is de inhoud van het huidige `extensions/odcn-registry-rewrite.yaml`. Op kind staat die tabel er niet en dan gebeurt er niets: geen herschrijving, geen secret. Dat een dienst platformfeiten uit de clusterconfig leest is bestaand gedrag, `publish-on-web` doet het met `get_ingress_postfix()` en vlam met `get_vlam_config()`.

**Gebruikersconfiguratie**: de private registries die een project zelf opgeeft, met upstream en token, plus de verwijzing per component.

**Eén bewerking**: `resolve_image()`, die beide bronnen als regels achter elkaar zet en de eerste match toepast. Zie de volgende sectie.

### Waarom niet twee diensten

Dit is overwogen: een `SYSTEM`-dienst voor het platformbeleid en een `USER`-dienst voor het private deel, met een volgorde ertussen. Dat is afgewezen nadat de regelvorm eruit kwam. Beide diensten zouden dan namelijk niets anders doen dan een lijst regels teruggeven, en dan bouw je een compleet dienstpakket, met een `ServiceType`, een definitie, een registratie, een schemafragment en tests, voor wat in de praktijk een tabel in de clusterconfig is. Het enige dat je ervoor terugkrijgt is zuivere `ServiceKind`s, en dat weegt niet op tegen een tweede pakket plus een ordening tussen pakketten die een lezer moet kennen om te snappen hoe een image wordt opgelost.

### Wat er dan nog schuurt, en hoe we dat wegnemen

De dienst is `USER`-kind, want de afnemer kiest hem bij een component en vult er registries in. Maar zijn clustertabel geldt ook voor projecten die hem nooit aanraken, want ook `nginx:alpine` heeft op ODCN een secret nodig. Drie dingen houden dat netjes:

1. **`resolve_image()` is een functie, geen haak.** Hij heeft geen staat en geen activatie, en de generator roept hem aan zoals hij vandaag ook een naamfunctie aanroept. Er is dus geen "dienst die draait zonder gekozen te zijn"; er is een functie die altijd hetzelfde antwoord geeft op dezelfde vraag.
2. **Wat wél activatie kent is alleen het provisioneren**, en dat is door data gedreven: geen registries in de config betekent geen `Organization`, geen secret, geen bijdrage aan het projectniveau.
3. **De selectie wordt afgeleid uit de data.** De dienst staat aan zodra er minstens één registry in staat; er komt geen aparte schakelaar. Dan bestaat "aangevinkt maar leeg" niet en wordt de clustertabel ook nergens als een keuze van de afnemer gepresenteerd.

### De provisioning-backend

Het enige dat per cluster echt verschilt is wat er moet worden aangemaakt voor een registry die een afnemer opgeeft:

```python
class RegistryBackend(Protocol):
    async def ensure(self, ctx, entry) -> None:   # registry klaarzetten, replay-safe
    async def remove(self, ctx, entry) -> None:
```

**`direct-secret`** voor kind, sandbox en elk cluster waar de nodes zelf bij de registry kunnen: `ensure` maakt een dockerconfigjson-secret uit de credentials, `remove` haalt het weg. Dat is letterlijk het pad dat `registries:` met username en password vandaag al volgt.

**`quay-proxy-organization`** voor ODCN en elk cluster met dezelfde operator: `ensure` schrijft het credentials-secret en de `Organization`, `remove` verwijdert de CR en daarmee de organisatie in RCR. De RCR-URL en de secretnaam volgen uit de naamfuncties, dus er valt niets terug te geven en niets op te wachten.

Een derde platform is een derde backend plus een tabel in de clusterconfig, en geen wijziging aan de dienst.

### Eén regelvorm, twee bronnen

De clustertabel en een private registry doen bijna hetzelfde, en dat is precies de reden om er niet twee mechanismen van te maken. Uitgeschreven is het dezelfde bewerking:

```
gedeeld:  match code.overheid.nl             -> to rcr.rijksapps.nl/code-overheid-rig
          code.overheid.nl/robbert/demo:tag  =>  rcr.rijksapps.nl/code-overheid-rig/robbert/demo:tag

privé:    match code.overheid.nl/robbert     -> to rcr.rijksapps.nl/codeoverheid-rig-<project>
          code.overheid.nl/robbert/demo:tag  =>  rcr.rijksapps.nl/codeoverheid-rig-<project>/demo:tag
```

Het namespace-segment dat bij de privévariant wegvalt is geen apart gedrag: het volgt uit een langere match. Eén regelvorm dekt allebei:

```python
@dataclass(frozen=True)
class RegistryRule:
    match: str    # upstream-prefix, met of zonder namespace
    to: str       # bestemming, host plus org
    secret: str   # het pull-secret dat bij die bestemming hoort
```

De dienst stelt die regels samen uit zijn twee bronnen: één regel per registry in de projectconfig, gevolgd door de clustertabel. Projectregels vooraan, want de eigen registry van een project moet winnen van de gedeelde proxy voor dezelfde upstream. Daarna past hij er één functie op toe, `resolve_image()`: normaliseer de image, loop de regels af, eerste match wint, vervang het gematchte deel door `to` en geef het secret terug. Geen match betekent image ongewijzigd en geen secret.

Daarmee is "wie wint" een gewone eerste-match op één lijst, en geen onderhandeling tussen mechanismen. Eén implementatie van het herschrijven, op één plek, gevoed door twee bronnen.

Drie dingen die in die ene functie horen en nergens anders:

1. **Normalisatie van korte namen.** `nginx:alpine` wordt eerst `docker.io/library/nginx:alpine`, anders matcht de regel `docker.io` hem niet terwijl de admission-webhook van ODCN hem wel zo behandelt. Gemeten: die webhook maakt er `rcr.rijksapps.nl/dockerhub-rig/library/nginx` van.
2. **Een image die al op de bestemming staat.** Begint hij al met de `to` van een regel, dan is er niets te herschrijven maar hoort er wel het secret van die regel bij. Dat is het dp-bn7-geval, en het is met deze vorm één regel code in plaats van een aparte `prefix:`-uitzondering.
3. **Volgorde is data, geen code.** De projectregels staan vooraan in de lijst, en dat is de hele voorrangsregeling.

Wat de afnemer hiervan merkt: niets. Hij vult een registry in en kiest hem bij een component. De rest is platform.

Een test die de twee bronnen uit elkaar houdt hoort er meteen bij: dezelfde `code.overheid.nl`-image levert mét een private registry de eigen proxy-org op met het eigen secret, en zónder die registry de gedeelde proxy met het robot-secret.

## Wat er met de bestaande rewrite-extensie gebeurt

Vandaag zit de omzetting in `RegistryRewriteExtension`, een manifest-extensie die na het genereren over de weggeschreven bestanden loopt (`project_manager.py:3975`, `extension_pipeline.process_directory`) met zijn mapping in `extensions/odcn-registry-rewrite.yaml`. Zodra `resolve_image()` bij het genereren draait, doet die extensie hetzelfde werk een tweede keer, en dan is de vraag welke wint.

De bedoeling is dat de extensie verdwijnt en zijn mapping opgaat in de clusterconfiguratie van de dienst. Eén eigenaar voor "welke registry, welk secret", op één moment, in plaats van drie mechanismen die elkaar aanvullen (de extensie, onze `imagePullSecretsMap`, en de erfenis van de serviceaccount).

Dat kan niet in één klap, want de extensie vangt vandaag ook images op die niet door de componentlus lopen. Elke plek waar een image in een manifest terechtkomt moet eerst `resolve_image()` gaan aanroepen. Dat zijn er meer dan je denkt, en dit is de lijst om af te vinken:

- `deployment.yaml.jinja` voor de componenten zelf
- de sidecars, waaronder de auth-wall proxy
- `postgresql-cluster.yaml.jinja` voor CNPG, waar de image uit de dienstconfig komt
- de backup-, restore- en snapshotpods
- de db-console pod en de losse jobpods

Volgorde: eerst `resolve_image()` overal aanroepen met de extensie nog actief als vangnet, dan vergelijken of de gegenereerde manifesten voor en na identiek zijn (de golden manifests dekken dat al af), en pas dan de extensie en zijn map opruimen. Zolang beide draaien is er geen conflict: na `resolve_image()` staat er al een RCR-pad en dat matcht geen enkele `from:`-prefix, dus de extensie laat het met rust.

Twee dingen die hierbij horen en die pas bij de meting van vanochtend zichtbaar werden:

- **korte namen moeten genormaliseerd worden.** Onze mapping matcht op `from: docker.io`, maar `nginx:alpine` begint daar niet mee. ODCN's admission-webhook behandelt hem wel als `docker.io/library/nginx`, en zonder dezelfde normalisatie mist zo'n image straks zijn secret.
- **onze mapping moet die van ODCN spiegelen.** Nu valt drift niet op omdat de serviceaccount alles opvangt. Straks is elk verschil een pull-storing. Vraag aan ODC-Noord of hun mapping ergens leesbaar is, zodat we hem kunnen toetsen in plaats van kopiëren.

Daarnaast loopt de weergavekant achter: `deployment_diagnostics` en `event_interpreter` gebruiken `original_image()` met de clustermappings om een gebruiker zijn eigen registry te tonen in plaats van de proxy. Die functie moet straks bij dezelfde dienst aankloppen, anders ziet een afnemer de kale RCR-URL.

## Projectniveau in de deployments-repo

Vandaag is de deployments-repo `<cluster>/<project>/<deployment>/`, en een projectmap bestaat alleen als verzamelmap zonder eigen manifesten. In de argo-applications-repo bestaat het projectniveau al wel: daar staat per project een map met de AppProject, de repo-secrets en één Application per deployment.

Er komt een projectniveau bij, want er zijn nu al bestanden die daar horen en die we per deployment dupliceren of buiten git om aanmaken:

- de ACME-issuer wordt per deployment weggeschreven (`issuer-letsencrypt-rijksapp-nl-production`, `-main`, enzovoort). Namespace-scoped, identiek, en alleen niet botsend omdat de deploymentnaam in de resourcenaam zit.
- de tenant-baseline netwerkpolicy staat per deployment in de map.
- de Namespace wordt helemaal niet via git gemaakt maar met kubectl door OPI zelf.
- en nu dus de `Organization` met zijn credentials-secret.

De vorm:

- **Deployments-repo**: een map `<cluster>/<project>/_project/` met een eigen `kustomization.yaml` en `decrypt-sops.yaml`. De naam is een voorstel; de underscore is bewust, want een deploymentnaam is een DNS-label en kan er geen bevatten, dus deze map kan nooit botsen met een deployment.
- **Argo-applications-repo**: in de bestaande projectmap een tweede Application die naar dat pad wijst, naast de AppProject die daar al staat. Sync-wave 0, terwijl de deployment-applicaties op wave 1 staan, zodat het projectniveau eerst gaat.
- **Naam van de applicatie**: `{project}-project` botst met een deployment die letterlijk `project` heet. Er bestaat vandaag geen lijst met gereserveerde deploymentnamen, dus die komt erbij en `project` is de eerste bewoner.
- **Prune**: een eigen regel in dezelfde vorm als de bestaande deployment-prune, dus op bestandsnaam-prefix per dienst, zodat het projectniveau leegloopt zodra een dienst uitgaat.
- **Wanneer het wordt geschreven**: alleen bij projectbrede gebeurtenissen, dus een project-refresh of een wijziging in de dienstconfig, niet bij elke deployment-taak. En schrijven met de bestaande skip-if-unchanged, anders herversleutelt SOPS zich suf en krijg je precies de churn die we eerder hebben opgeruimd.
- **Verwijderen**: bij het verwijderen van een project moeten de map en de extra Application mee, langs hetzelfde pad dat de AppProject nu al opruimt.

Ordening en gereedheid zijn twee verschillende dingen. De sync-wave regelt alleen de volgorde van aanmaken, niet of de proxy al klaar is. Dat hoeft ook niet strikt: een pod die te vroeg is belandt in ImagePullBackOff en herstelt vanzelf zodra de proxy werkt. Dat is in de proef letterlijk gebeurd, de pod kwam op `Running` zodra de credentials klopten, zonder dat iemand hem aanraakte. Wat we wel doen is `proxyCache.ready` op de detailpagina tonen, zodat een wachtende afnemer ziet waarom hij wacht.

### Een haak, geen hardgecodeerde schrijver

Het projectniveau krijgt dezelfde vorm als het deploymentniveau al heeft, dus een haak die diensten aanbieden in plaats van code die weet welke bestanden er moeten komen. Naast `contribute_deployment_manifests` komt er `contribute_project_manifests`, met een `ProjectManifestSpec` die een template, waarden en een bestandsnaam noemt, en die bestandsnaam begint verplicht met de dienstnaam zodat de symmetrische prune hem weer kan weghalen als de dienst uitgaat. De registry verzamelt de diensten die hem overschrijven in `manifest_order` en de generieke emitter schrijft ze weg. Geen nieuw begrip dus, een tweede exemplaar van een bestaand begrip.

Dat is ook de reden om het nu goed te doen en niet één keer speciaal voor deze dienst. De issuers, de baseline-netpol en de namespace zijn vandaag hardgecodeerd op plekken die er niets mee te maken hebben. Met deze haak krijgen ze later een eigenaar: de dienst die het ding nodig heeft draagt het bij, en het generieke pad hoeft er niets van te weten. Dat verplaatsen zit bewust niet in dit plan, de haak wel.


## Eigen serviceaccount per project

De `default` serviceaccount is geen vangnet dat we willen houden, om twee redenen.

De eerste is toegang: hij draagt elk pull-secret dat ODCN in de namespace repliceert, dus ook dat van de proxy-organisatie van een ander project. Zolang onze pods daarop draaien is een private registry alleen op papier privé.

De tweede is dat het ook technisch geen goed idee is. Alle secrets wijzen naar dezelfde host, `rcr.rijksapps.nl`, en kubelet moet daar de juiste uit halen. Bij negen secrets is dat al onzeker, bij honderd is het een probleem. Het verklaart bovendien de dp-bn7-storing: die pod faalde met `invalid username/password` terwijl het juiste secret volgens ODCN's eigen release note op de `default` serviceaccount stond. Dat past bij "eerste match wint" en niet bij "probeer ze allemaal". Hard bewezen is dat niet, maar het is het enige verhaal dat bij de waarneming past, en het maakt de erfenis onbetrouwbaar in plaats van alleen rommelig.

Daarom: een eigen serviceaccount per project, zonder pull-secrets, als tweede bewoner van het projectniveau uit dit plan. En elke gegenereerde podspec draagt de secrets die hij zelf nodig heeft, geleverd door `resolve_image()`. Meestal is dat er precies één.

**Gemeten op 2026-09-07**, met een lege serviceaccount `probe-empty` in `rig-prd-test` en drie pods:

| Pod | Image zoals ingediend | Uitkomst |
|---|---|---|
| lege SA, publieke image | `alpine:3.20.3` | ImagePullBackOff, `authentication required` |
| lege SA, RCR-pad | `rcr.rijksapps.nl/dockerhub-rig/library/alpine:3.20.3` | ImagePullBackOff, `authentication required` |
| lege SA, RCR-pad plus expliciet secret | idem, met `dockerhub-rig-robot-pull-secret` | Succeeded |

De eerste rij is de belangrijkste: ik diende `alpine:3.20.3` in en in de podspec stond na admission `rcr.rijksapps.nl/dockerhub-rig/library/alpine:3.20.3`, inclusief het `library/`-segment. Dat is de admission-rewrite die ODCN op 10 augustus aankondigde. Gevolg: elke image belandt op een RCR-pad, elk RCR-pad vraagt authenticatie, en dus heeft élke pod een secret nodig, ook voor een doodgewone publieke image.

De derde rij is het bewijs dat het kan: een expliciet secret in de podspec volstaat, zonder enige erfenis. En een niet-default serviceaccount werkt hier sowieso al, want `rig-db-1` draait op serviceaccount `rig-db` met twee expliciete pull-secrets uit de infrastructuur-kustomize.

De volgorde is hier de hele beslissing. De serviceaccount gaat pas aan als `resolve_image()` overal wordt aangeroepen en elke gegenereerde podspec zijn eigen secret draagt, inclusief de genormaliseerde korte namen. Andersom haal je het vangnet weg voordat het net eronder gespannen is, en dan breekt elke component met `nginx:alpine` tegelijk.

Twee dingen om bij het bouwen te toetsen: dat OpenShift de gebruikelijke SCC aan de nieuwe serviceaccount bindt, en wat er gebeurt met operators die hun eigen serviceaccount maken, zoals CNPG met `rig-db`. Die laatste categorie draait niet op onze serviceaccount, dus daar moet het secret in de resource-spec zelf staan.

## Naamgeving, en waar hij woont

Alle afgeleide namen lopen door de bestaande normalisatie in `opi/utils/naming.py`, zoals elke andere naam in ZAD dat al doet. Wat nieuw is, zijn de namen die alleen deze dienst kent: de `friendlyName` uit de upstream-host, de organisatienaam, de naam van het pull-secret en de omzetting van een upstream-image naar zijn RCR-vorm.

Die vier horen in het dienstpakket zelf, in een eigen `naming.py` in `opi/services/catalog/private_registries/`, en niet in de centrale `opi/utils/naming.py`. Reden: de maatstaf uit `plans/alles-van-een-dienst-in-een-map.md` is dat je de map moet kunnen kopiëren, hernoemen en dat het dan werkt. Naamregels die alleen over deze dienst gaan in een gedeeld bestand zetten is precies wat dat onmogelijk maakt. De centrale helpers worden wel gebruikt (`sanitize_kubernetes_name`, `generate_resource_identifier`), ze worden niet nagebouwd. Andere dienstpakketten doen dit al zo, bijvoorbeeld `publish_on_web/urls.py` en `vlam/endpoint.py`.

Vier regels die daarmee op één plek staan:

- `friendlyName` is de upstream-host zonder punten, want Quay verbiedt een punt in dat veld.
- de organisatie heet `<friendlyName>-<customerName>-<project>`, waarbij de eerste twee delen door de operator worden samengesteld en het derde onze `suffix` is.
- het pull-secret krijgt een naam die wij expliciet zetten en die de projectnaam draagt, omdat de operator anders de suffix weglaat en twee projecten met dezelfde upstream op één naam botsen.
- de image-omzetting vervangt de host en haalt het namespace-segment uit het pad, dus `code.overheid.nl/robbert.uittenbroek/demo:tag` wordt `rcr.rijksapps.nl/<org>/demo:tag`.

Deze vier zijn **berekeningen, geen opgeslagen waarden**. Dat is de reden dat het projectbestand alleen de invoer van de afnemer bevat: alles wat wij eraan toevoegen zou een tweede waarheid zijn die kan gaan afwijken van de organisatie zoals die er werkelijk staat. Waar de manifestgeneratie vandaag de `url` en `secretName` uit de entry leest, vraagt hij ze straks aan de backend van de dienst. Dat verplaatst de kennis naar de plek die hem hoort te hebben en scheelt een terugschrijfpad.

Er is daarmee ook geen poort nodig die wacht tot de organisatie klaar is voordat een deployment mag uitrollen. Een pod die te vroeg is gaat in ImagePullBackOff en herstelt vanzelf, precies zoals in de proef gebeurde.

## Beslissingen

**D1. Er komt een projectniveau in de deployments-repo met een eigen ArgoCD-applicatie, en de `Organization` gaat daarin.**
De eerste versie van dit plan stelde voor de CR met kubectl aan te maken, zoals de namespace vandaag gaat. Dat is afgewezen: projectbrede bestanden komen vaker terug (de issuers, de baseline-netpol, de namespace zelf), en zonder die laag blijven we per geval een uitzondering verzinnen. Zie de sectie hierboven voor de vorm, en let op dat het via een haak gaat die elke dienst kan gebruiken, niet via een schrijver die deze ene dienst kent.

**D2. Eén organisatie per project per upstream-namespace.**
Naam `<friendlyName>-<customerName>-<project>`. De secretnaam zetten we expliciet, want de operator leidt hem af zonder de suffix en dan botsen twee projecten met dezelfde upstream op één naam, tenantbreed.

**D3. De koppeling is componentconfig van de dienst, geen sleutel op de component.**
`registry:` als directe sleutel naast `image:` gaat eruit. De component vermeldt de dienst met een configblok, zoals elke andere componentgebonden dienst dat doet. Dat levert het veld gratis op via de drie componenthaken, het laat het veld verdwijnen zodra de dienst uitgaat, en het maakt "publiek" een echte non-waarde: geen vermelding, geen sleutel. Puur afleiden uit de image-URL is geen alternatief, want dat faalt in twee gevallen die we allebei hebben meegemaakt: twee registries met dezelfde URL en verschillende tokens, en een image zonder enige verwijzing (de dp-bn7-storing).

**D4. De lijst verhuist van root naar de dienstconfig, met een inventaris van wat er mee moet.**

Een verhuizing is pas af als alles wat op de oude plek gold ook op de nieuwe plek geldt. Twee lijstjes horen daarom in de taak, en het ontbreken ervan heeft in de eerste bouwronde twee blokkerende securitybevindingen opgeleverd.

*Beperkingen die mee moeten verhuizen*, af te lezen uit `$defs/registry` op de basis:

| Wat | Op de oude plek | Waarom het telt |
|---|---|---|
| patroon op `url` | `^(?:(?:https?\|ssh\|git)://)?[^\s\u0000"]+\Z` | verbiedt aanhalingsteken en regeleinde; zonder dit rendert een upstream als extra YAML-documenten in het manifest |
| `password` is `age-encrypted-or-plain` | AGE-patroon | `find_plaintext_secret_violations` matcht op precies dat patroon en draait fail-closed op elke schrijfroute; een kale string zet die grendel uit |
| `name` uniek binnen de lijst | validator | verwijzingen vanaf componenten lossen op naam op |

*Lezers van het veld*, want elke lezer moet dezelfde uitpakking gebruiken (`carries_encrypted_value` plus `decrypt_password_smart`), en er is er altijd eentje meer dan je denkt: de provisioning-backend, de formulierconverter, de enforcer die het token toetst, en `project_manager.py`. Inventariseer ze vóór de wijziging en noem ze in de PR.


Root-`registries:` heeft nu geen eigenaar en daarmee geen formulier, geen configmodel, geen schemafragment en geen validatie. Schemaversie omhoog en de twee bestaande projecten meenemen in de fixup. `dp-bn7` migreert waarschijnlijk naar niets: die entry is een noodverband voor de platformregistry en hoort thuis in de auto-wiring uit `features/futures/platform-registry-pull-secret-wiring.md`. Van `algor-odc` eerst nameten of die ghcr-credentials iets doen; als het pakket publiek is, werken ze niet eens en verdwijnen ze.

**D5. Het token wordt bij het opslaan getoetst.**
Een tokenuitwisseling plus `tags/list` tegen de upstream, vanuit OPI, zoals we hem in de proef met de hand deden. Anders komt een te smal token pas naar boven als `ImagePullBackOff` met de melding `repository not found`, en die wijst de verkeerde kant op.

**D6. De eigendomscontrole wordt uitgebreid.**
`validate_platform_registry_image_ownership()` kijkt vandaag alleen naar `{REGISTRY_URL}/{REGISTRY_ORG}`. Omdat wij de proxy-organisaties zelf aanmaken, weten we welke bij welk project hoort, en kan hetzelfde schrijfpunt een verwijzing naar de proxy-org van een ander project weigeren. Dat is nodig, want elke pod in de tenant krijgt het gerepliceerde pull-secret via de `default` serviceaccount en kan dus elk pad onder zo'n org opvragen, waarna RCR met andermans credentials bovenstrooms ophaalt.

**D7. Eigen serviceaccount per project, maar pas nadat `resolve_image()` overal draait.**
De `default` serviceaccount draagt elk gerepliceerd pull-secret, dus daarop draaien maakt een private registry alleen op papier privé, en kubelet moet uit negen kandidaten voor dezelfde host de juiste vissen. Gemeten is dat een lege serviceaccount elke pull laat mislukken, ook van een gewone publieke image, omdat het cluster alles naar RCR omleidt, en dat een expliciet secret in de podspec volstaat. Dus eerst `resolve_image()` overal aanroepen inclusief normalisatie van korte namen, dan pas de serviceaccount. Zie de sectie hierboven.

**D8. Verwijderen mag direct.**
Bij een proxy cache verliest de afnemer alleen de cache; bovenstrooms staat alles er nog. Dat is iets anders dan een registry-organisatie met gepushte images, waar verwijderen onherstelbaar zou zijn. Wel expliciet melden in de UI wat er gebeurt.

**D9. Eén dienst `image-registries`, met de clustertabel en de gebruikersconfig als twee bronnen van dezelfde regellijst.**
Onderweg zijn drie vormen overwogen. Eén dienst met een altijd-draaiend deel en een kiesbaar deel, wat een dienst opleverde die je aanvinkt terwijl hij ook zonder aanvinken werkt. Twee diensten met een volgorde ertussen, wat een compleet tweede pakket kost voor wat een tabel is. En deze: één dienst, twee bronnen, één regellijst, één functie. De doorslag gaf dat het platformbeleid en een private registry bij nader inzien exact dezelfde bewerking zijn, alleen met een langere of kortere match, dus er valt niets te scheiden dat het scheiden waard is. De naam gaat over het onderwerp en niet over de functie, zodat de kaart niet "private" belooft terwijl de dienst ook publieke images regelt.

## Fasering

1. **Model en dienstpakket.** De dienst met configmodel, editables, visualizers en schemafragment op twee lagen: projectconfig voor de registries, componentconfig voor de verwijzing. Migratie: root `registries:` naar de dienstconfig, en de drie bestaande `registry:`-sleutels op deployment-componenten naar de nieuwe vorm, waarna die sleutel uit het schema gaat. Verifieer: test die aantoont dat de componentkeuze erft en de deployment-override wint in `imagePullSecretsMap`, plus een migratietest op de twee bestaande projectbestanden.
2. **Het projectniveau en de backend `direct-secret`, plus de UI.** De haak `contribute_project_manifests`, de projectmap met zijn eigen ArgoCD-applicatie, en de eerste bewoner: het dockerconfigjson-secret, dat namespace-scoped is en vandaag nog per deployment wordt geschreven. Wizardsectie voor de registries, select op de component met options-provider en `values_must_exist`, afleiding van de upstream uit de image. Verifieer: op de sandbox een private image binnenhalen, end-to-end via de wizard.
3. **Backend `quay-proxy-organization`.** Clusterconfigsleutel, de CR en het credentials-secret, de omzetting bij het genereren zodat de cluster-rewrite hem niet meer ziet, `remove`, en `proxyCache.ready` op de detailpagina. Verifieer: op productie hetzelfde resultaat als de handproef, maar nu via ZAD, met een project dat de dienst aanzet.
4. **De clustertabel en de eigen serviceaccount.** De mapping uit `extensions/` naar de clusterconfig van de dienst, en elke plek die een image in een manifest zet gaat langs `resolve_image()` (componenten, sidecars, CNPG, backup, restore, db-console, jobs), met normalisatie van korte Docker Hub-namen en de extensie nog actief als vangnet. Daarna de serviceaccount per project, en pas als die staat de extensie en zijn `extensions/`-map opruimen. Verifieer: de golden manifests blijven identiek terwijl de extensie nog draait, en daarna draait een deployment op de eigen serviceaccount met precies één pull-secret per image.
5. **Validaties en randen.** Tokentoets bij opslaan, uitgebreide eigendomscontrole, helptekst over quota en rotatie, en een melding als het token bijna verloopt. Verifieer: een te smal token geeft een leesbare fout in het formulier en geen enkele pod.

## Vragen aan ODC-Noord

1. Kan `pullsecrets-rig` de organisaties overslaan die wij zelf aanmaken, of kan de replicatie per organisatie uit? Nu krijgt elke namespace in de tenant het pull-secret van elk project.
2. De secretnaam die de operator kiest laat `spec.suffix` weg, waardoor twee organisaties dezelfde secretnaam krijgen. Is dat bedoeld?
3. De documentatie zegt bij `rotation.enabled: false` "it will never expire/rotate", maar de controller zet alsnog een `tokenExpiryDate` op 90 dagen. Documentatie of gedrag klopt niet.
4. Hoeveel proxy-organisaties verdraagt RCR per klant, en telt hun quotum mee in de 50 GB van de klant?
5. Kan jullie admission-rewrite een proxy-organisatie in de eigen namespace voorrang geven boven de gedeelde proxy? Nu stuurt hij een private upstream naar de gedeelde cache, waar geen credentials op zitten, en daarom moeten wij zelf herschrijven. Kan het aan jullie kant, dan vervalt onze omzetting helemaal.
6. In de gedeelde `code-overheid-rig` staat nog een image uit de tijd dat het pakket publiek was, terwijl het bovenstrooms nu privé is. Hoe gaan jullie om met zulke restanten?

## Wat we bewust niet doen

- Geen eigen registry hosten. Wij zijn de proxy, niet de bewaarplaats.
- Geen `RegistryProxy`-CRD van onszelf zoals in `features/futures/registry-proxy.md`. De `Organization` van ODCN doet al wat we nodig hebben en een eigen laag ertussen levert niets op.
- Geen mutating webhook die pull-secrets injecteert. Wat in de podspec hoort, zetten wij daar zelf neer.
