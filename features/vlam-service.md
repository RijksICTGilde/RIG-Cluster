# VLAM-API binnen het cluster (dienst `vlam`)

De ZAD-dienst `vlam` geeft een project toegang tot de VLAM-API van SSC-ICT vanuit zijn eigen pods,
zonder VPN en zonder dat de afnemer zelf een certificaat hoeft te vertrouwen.

```
afnemer-pod --http--> vlam-proxy-intern:8081 --https (SNI + CA-verificatie)--> vlam-api.rijksweb.nl
```

De dienst is bewust dun. Hij levert een adres en een netwerkregel; alles wat daar aan de andere kant
van hangt (de proxy, de RON-koppeling, de CA-keten) is beheer van het `vlam-wt8`-project en staat in
`vlam.md`.

## Wat je krijgt

Zet je de dienst aan, dan krijgt **elk component van elke deployment** van je project:

| | |
|---|---|
| `VLAM_API_URL` | het adres van de interne VLAM-proxy, bijvoorbeeld `http://productie-vlam-proxy-intern.rig-prd-vlam-wt8.svc.cluster.local:8081` |
| een uitgaande netwerkregel | van de pods van je deployment naar precies die ene proxy-pod |

En op een cluster dat het **doorlus-pad** aanbiedt (RC-167) daarnaast:

| | |
|---|---|
| `VLAM_API_URL_DIRECT` | het adres van de doorlus-poort, met de VLAM-hostnaam erin: `https://vlam-api.rijksweb.nl:8443` |
| `VLAM_CA_BUNDLE_PATH` | `/etc/ssl/vlam/rijksdienst-ca.pem`, de uitgever waartegen je het certificaat van VLAM verifieert |
| een regel in `/etc/hosts` | die de VLAM-naam naar het ClusterIP van onze proxy wijst (`hostAliases`) |
| het bestand zelf | als Secret per deployment, read-only gemount op dat pad |

Er zijn geen instellingen. Er valt niets te kiezen: een endpoint, drie variabelen, een regel.

Verwacht je bibliotheek een andere naam dan `VLAM_API_URL`, gebruik dan een alias op je component.

## Het doorlus-pad (RC-167)

Poort 8443 van dezelfde proxy lust de TLS-sessie DOOR in plaats van hem te termineren. De afnemer
praat dan zelf met VLAM en verifieert zelf het certificaat; ZAD ziet dat verkeer niet meer. De poort
lag er sinds RC-142, maar was niet bruikbaar zonder handwerk -- en dat handwerk is precies het soort
dat afnemers verkeerd doen.

Er zijn drie dingen nodig, en ze zijn alle drie waardeloos zonder de andere twee:

| wat | waarom | zonder dit |
|---|---|---|
| het **adres** van de doorlus-poort | de afnemer moet weten waar hij heen moet | hij kent 8443 niet |
| de **naam** die naar onze proxy wijst | TLS vergelijkt de hostnaam uit de URL met het certificaat | certificaatfout op elke verbinding |
| het **CA-certificaat** in de pod | hij verifieert nu zelf, en kent de uitgever niet | onbekende uitgever, verbinding faalt |

Daarom levert de dienst ze als EEN geheel: `VlamEndpoint.passthrough` is er of is er niet, nooit voor
twee derde. Een cluster biedt de doorlus aan zodra zijn `vlam`-blok `passthrough_port`, `api_host`,
`cluster_ip` en `ca_bundle` noemt EN het genoemde bestand in de dienstmap staat.

### Waarom het certificaat van het platform komt en geen bijlage is

Het is voor elke afnemer identiek en verandert alleen als VLAM van uitgever wisselt. Als bijlage zou
hetzelfde publieke bestand AGE-versleuteld in elk projectbestand terechtkomen, en zou roteren
betekenen dat iedereen opnieuw moet uploaden. Levert de dienst het, dan is roteren een wijziging op
een plek. Wat wel wordt hergebruikt is de MACHINERIE van bijlagen (`attachment_secret_mounts`:
Secret -> volume -> `volumeMount`), zodat er geen tweede manier ontstaat om een bestand in een pod te
krijgen. Die bijdrage is additief, dus de eigen bijlagen van een component blijven staan.

Het clusterbrede `ca_certificate`-mechanisme is bewust NIET gebruikt: dat zet een CA op elke
Deployment op het cluster, staat alleen op het lokale Kind-cluster aan en mount via een `hostPath`
van de node.

### Waarom de taalstandaard-variabelen NIET gezet worden

Het is verleidelijk om ook `REQUESTS_CA_BUNDLE`, `SSL_CERT_FILE` of `NODE_EXTRA_CA_CERTS` te zetten
zodat het vanzelf werkt. De eerste twee **vervangen** de vertrouwensketen in plaats van hem aan te
vullen: een pod met `SSL_CERT_FILE` op alleen deze bundel vertrouwt verder niets meer -- geen publieke
API, geen Keycloak, geen pakketspiegel -- en die storing lijkt in niets op zijn oorzaak. De dienst kan
ook geen betrouwbare samenvoeging maken, want hij kent het basis-image van de afnemer niet. Dus: een
pad aanbieden, de applicatie laat kiezen, en per taal in `help.md` uitleggen hoe je een CA er NAAST
zet.

Dat verschil zit ook in de code: `VariableDefinition.conditional` markeert de twee doorlus-variabelen
als "hangt aan het cluster, niet aan de binding", zodat de e2e-probe ze niet in elke pod eist.

### Het ClusterIP is een geconfigureerde waarde, en dat is een aanvaard risico

`hostAliases` neemt een IP-ADRES, geen servicenaam, en de Service-template van ZAD zet geen
`clusterIP`. Het adres van `productie-vlam-proxy-intern` is dus dynamisch toegewezen en staat als
waarde in `cluster_config.py`, op een plek om te wijzigen.

Waarom dat verantwoord is: een ClusterIP is onveranderlijk zolang de Service bestaat, en verschuift
alleen als die Service verwijderd en opnieuw aangemaakt wordt. Gebeurt dat toch, dan faalt het
VEILIG -- TLS valideert de hostnaam, dus verkeer dat bij een andere dienst uitkomt strandt op een
certificaatfout in plaats van ergens verkeerd bezorgd te worden.

Het adres opzoeken tijdens het genereren van de manifesten is geen alternatief: gegenereerde
manifesten staan in git en zouden dan stilzwijgend verouderen zonder dat iemand het ziet.

Overwogen en afgevallen: `vlam-api.rijksweb.nl` clusterbreed naar onze proxy laten wijzen. Dan hoeft
er bij afnemers niets, maar kan onze eigen proxy zichzelf niet meer opzoeken en moet het echte
VLAM-adres in onze backend hardgecodeerd worden -- precies wat `resolvers dns` daar oplost, want VLAM
is eerder van IP gewisseld en dat leverde 500'en op.

### De CA-bundel downloaden

Op de projectpagina, tabblad Services info, staat het blok VLAM-API met beide adressen, het pad in de
pod en een knop die de bundel ophaalt (`GET /services/vlam/ca-bundle`, achter de login zoals elke
andere route van de app). Van de drie dingen die de dienst neerzet is het bestand het enige dat je
zonder de pod niet kunt bekijken, en het is het ding dat het vaakst verkeerd begrepen wordt.

## Toegang: eenmalig aan de VLAM-kant, daarna is afnemen genoeg

De VLAM-proxy is een gedeelde voorziening: wie hem mag gebruiken is niet een korte, bekende
lijst maar "ieder project dat de dienst aanzet". Daarom staat er aan de VLAM-kant EEN regel,
eenmalig gezet, die poort 8081 van `vlam-proxy-intern` zonder projectlimiet openzet:

```yaml
  - name: cross-domain-access
    schema-version: "1.1"
    config:
      inbound:
        - name: iedereen-in-het-cluster
          from: { project: "*" }        # geen projectlimiet
          to: { component: vlam-proxy-intern, port: 8081 }
```

Sinds RC-167 heeft de doorlus daar een TWEEDE regel van dezelfde vorm nodig, want een
inbound-regel noemt een poort en niet een reeks:

```yaml
        - name: doorlus-iedereen-in-het-cluster
          from: { project: "*" }
          to: { component: vlam-proxy-intern, port: 8443 }
```

Die regel staat in het projectbestand van `vlam-wt8` en niet in deze repo. Ontbreekt hij, dan zet
de dienst bij de afnemer wel de uitgaande weg open en strandt het verkeer aan de VLAM-kant.

Voor een afnemer betekent dat: **de dienst aanzetten is genoeg**. Je krijgt het adres en de
uitgaande regel, en de proxy laat je binnen. Er hoeft niemand meer een regel per afnemer bij
te houden -- dat zou de eigenaar van een gedeelde voorziening tot poortwachter van een
zelfbedieningsplatform maken.

Wat dat kost, expliciet: op poort 8081 is de proxy bereikbaar voor elke bron die er een
netwerkpad heen heeft. De grens die overblijft is de uitgaande kant (zonder de dienst heeft
je pod geen weg naar die namespace) en, daarachter, **de autorisatie van VLAM zelf**: VLAM
controleert de API-sleutel van de aanroeper. De netwerkregel is dus niet meer de
authenticatie; hij is de bereikbaarheid.

De wildcard geldt alleen voor die ene poort van dat ene component, alleen INKOMEND en alleen
op een inbound-regel. Uitgaand bestaat hij niet: een project dat zichzelf "overal heen" zou
geven is een gat, geen voorziening.

## Drie smaken, en waarom ze zo zijn

Er zijn drie paden naar VLAM, en ze bestaan naast elkaar:

| | VPN-pad (poort 8080) | deze dienst, getermineerd (8081) | deze dienst, doorlus (8443) |
|---|---|---|---|
| voor | mensen op een laptop | workloads in het cluster | workloads met een versleutelingseis |
| TLS | end-to-end, niet getermineerd | getermineerd op de proxy | end-to-end, niet getermineerd |
| CA-probleem | lost de gebruiker zelf op | een keer opgelost, op de proxy | de dienst levert de bundel, de applicatie gebruikt hem |
| toegang | Keycloak-login met rolfilter | netwerkregel + de API-sleutel van VLAM | netwerkregel + de API-sleutel van VLAM |

De keuze voor terminatie is de kern van het GETERMINEERDE pad, en het pad dat je hoort te kiezen tenzij je
een reden hebt om dat niet te doen. Het certificaat van `vlam-api.rijksweb.nl` komt
van `Rijksdienst Issuing CA2` en zit in geen enkele publieke bundel, dus zonder terminatie moet elke
afnemer die keten in zijn eigen runtime vertrouwen. Dat is per taal anders, en in de meeste runtimes
VERVANGT `SSL_CERT_FILE` de hele bundel, waarmee je andere HTTPS-verkeer van diezelfde applicatie
sloopt. Eén keer goed op de proxy is beter dan tien keer bijna goed bij de afnemers.

**De prijs, expliciet:** tussen de afnemer-pod en de proxy is het verkeer niet versleuteld. Dat
blijft binnen ons cluster en ons beheer, en de hop naar buiten is wel versleuteld en geverifieerd.
Wie versleuteling tot aan VLAM zelf nodig heeft, gebruikt sinds RC-167 het doorlus-pad hieronder --
en neemt daarmee het CA-probleem terug naar zijn eigen applicatie, met de bundel die de dienst
meelevert. Het VPN-pad blijft bestaan, voor laptops.

**Geen eigen certificaat op de interne hop.** Dat zou het vertrouwensprobleem alleen verplaatsen
naar de afnemer, met precies de `SSL_CERT_FILE`-valkuil hierboven. Intern HTTP met netwerkregels als
toegangscontrole is hier de eerlijkere keuze.

## Waar de dienst bestaat

Alleen op een cluster waarvan de configuratie een VLAM-endpoint kent (`vlam` in
`opi/core/cluster_config.py`). Dat is vandaag `odcn-production`, want daar bestaat de RON-koppeling.
De sandbox draagt een PLAATSHOUDER, zodat de bedrading (kaart, variabele, netwerkregel) daar
end-to-end te doorlopen is; er zit geen VLAM achter.

Sinds RC-144 zet de sandbox-E2E-suite zelf een STUB op die plaatshoudercoordinaten
(`tests/e2e/helpers/vlam_stub.py`): een haproxy die op `/v1/models` een vaste modellenlijst
teruggeeft, met dezelfde naam, namespace en pod-labels die het endpoint noemt. Daarmee is in de
sandbox niet alleen de bedrading maar de hele KETEN te meten -- de aanroep vanuit een afnemer-pod
komt aan, en een pod in een project zonder de dienst loopt op hetzelfde adres vast.

De stub wordt met `kubectl` neergezet en niet als ZAD-project aangemaakt, en dat is geen luiheid:
de plaatshouder noemt het project `vlam-wt8`, die naam staat in het pod-label waar de uitgaande
regel van de afnemer op selecteert, en een technische projectnaam is op dit platform niet te
kiezen -- `generate_project_name()` hangt er op ELKE aanmaakweg een willekeurig postfix van drie
tekens achter. De INKOMENDE regel van de stub wordt wel door de dienst zelf gerenderd
(`contribute_deployment_manifests` van cross-domain-access), zodat de sandbox de echte
wildcard-YAML afdwingt en niet een handgeschreven kopie ervan.

Op een cluster zonder endpoint gebeuren twee dingen, en allebei zijn nodig:

1. de dienstkaart staat niet in de wizard;
2. een project dat de dienst tóch selecteert wordt bij het opslaan geweigerd, met de clusternaam in
   de melding.

Alleen de kaart weglaten is geen validatie: de API en een met de hand geschreven projectbestand zien
nooit een kaart.

De endpointgegevens (project, deployment, component, namespace, poort) staan in de clusterconfiguratie
en niet in de code van de dienst. Het adres en de netwerkpeer worden er ALLEBEI uit afgeleid, zodat
ze niet uit elkaar kunnen lopen — een adres dat de ene pod noemt terwijl de regel een andere opent
komt bij de afnemer aan als een time-out en is dagen later pas te herleiden. Verhuist VLAM ooit naar
een platformbeheerde opzet, dan verandert alleen dat blok.

## Wat het onder water doet

| | |
|---|---|
| variabele | `ManifestContribution.env_vars` — additief, dus de eigen variabelen van het component blijven staan. Geen geheim: een intern adres versleutelen maakt het alleen onleesbaar voor de eigenaar. |
| netwerkregel | `contribute_deployment_manifests`, één `NetworkPolicy` per deployment, egress-only, `podSelector` op `deployment` + `project`. Opent ELKE poort van de proxy waarvoor de afnemer een adres kreeg (`VlamEndpoint.ports`) — een adres dat de netwerkregel niet opent is een time-out |
| alias en bestand | `template_vars["host_aliases"]` (override, niets anders zet die sleutel) en `ManifestContribution.secret_mounts` (additief) plus een `SecretFileSpec` met de inhoud van de bundel |
| uitzetten | de bestandsnaam draagt het prune-voorvoegsel `{deployment}-vlam-`, dus de generieke opruiming haalt de regel weg zodra de dienst niet meer bijdraagt |
| aanzetten | de PROJECTselectie, niet een vinkje per component (`manifest_activated_by_project`) — de dienst is deployment-gebonden, dus geen enkel component vinkt hem ooit aan |

## Wat er in cross-domain-access voor bij moest

De open kant vroeg om iets dat `cross-domain-access` nog niet kon: een inbound-regel zonder
peer. Dat is er nu, als **wildcard-peer** (`config_schema_version` 1.1):

- `from: { project: "*" }` op een INBOUND-regel betekent "geen projectlimiet"; de regel
  rendert als ingress-entry zonder `from`-selector, op alleen de genoemde poort.
- `deployment` en `component` moeten dan LEEG zijn. Een wildcard die er toch een noemt wordt
  geweigerd, niet stil genegeerd: zo'n regel leest als beperkt tot dat component en is dat
  niet.
- Uitgaand kent de wildcard niet; het model weigert hem daar.
- De keuzelijst in het formulier BIEDT de wildcard niet aan -- dit is een besluit van de
  eigenaar van een gedeelde voorziening, via de API of het projectbestand, geen menu-item.
  Een regel die hem al draagt wordt wel getoond, als "Geen projectlimiet (elke bron)", en de
  velden voor peer-deployment en peer-component verdwijnen dan uit die rij. Anders zou het
  verplichte peer-component het opslaan van elke andere regel van dat project blokkeren.

## Open punten

- **Herleidbaarheid richting SSC-ICT.** In-cluster afnemers zijn workloads, geen personen achter
  SSO. Al het verkeer komt bij SSC-ICT vandaan als één bron. Of dat acceptabel blijft, en of er per
  afnemer iets herleidbaars mee moet, is een gesprek met SSC-ICT en geen technische openstaande post.
- **Capaciteit.** De interne proxy deelt de RON-koppeling met de VPN-gebruikers. Wordt het
  workload-verkeer groot, dan is dat een quotagesprek met SSC-ICT, niet iets dat met `maxconn` op te
  lossen is.
- **De inkomende regel voor 8443 in `vlam-wt8`.** De dienst opent de uitgaande weg naar beide
  poorten; de inkomende wildcard aan de VLAM-kant bestaat vandaag alleen voor 8081. Die tweede regel
  hoort bij de eerste keer dat het doorlus-pad echt gebruikt wordt.
- **De CA-bundel zelf.** `opi/services/catalog/vlam/rijksdienst-ca.pem` is het bestand dat de
  productieconfiguratie noemt. Zolang het er niet staat biedt de dienst het doorlus-pad NIET aan:
  geen tweede adres, geen alias, geen mount, geen downloadknop. De keten van RC-142 blijft gewoon
  werken. Zodra iemand met toegang tot `rig-prd-vlam-wt8` de keten uit de bijlage van
  `vlam-proxy-intern` (`/etc/haproxy/rijksdienst-ca.pem`) daar neerzet, gaat de rest vanzelf aan.
- **Verloopt het geconfigureerde ClusterIP ooit stilzwijgend?** Bewust uitgesteld. Twee latere
  mogelijkheden, geen van beide nu nodig: een controle die het geconfigureerde adres vergelijkt met
  de draaiende Service en klaagt bij verschil, of alsnog een vaste `clusterIP`, wat een kleine
  uitbreiding van ZAD vraagt.
- **Welke naam zet de alias?** `vlam-api.rijksweb.nl` en `vlam-api.overheid-i.nl` werken allebei bij
  VLAM. De keuze is `rijksweb.nl` geworden, omdat dat de naam is waar deze hele keten op gebouwd is
  (de SNI-ACL van de proxy, het certificaat, het runbook) en de enige van de twee die publiek
  resolvet. Het is een expliciete keuze en geen bijproduct van volgorde; hij staat als `api_host` in
  de clusterconfiguratie en is daar met een regel te wijzigen.
- **`chat.rijksweb.nl` ontsluiten.** Kan (tweede frontend op 8082 met een eigen vaste backend en een
  `VLAM_CHAT_URL`), maar pas als er een concrete afnemer voor is.

## Zie ook

- `vlam.md` — het runbook van de gateway en van `vlam-proxy-intern` zelf
- `features/futures/vlam-api-vpn-proxy.md` — het ontwerp achter het VPN-pad
- `instructions/services.md` — hoe een dienst in elkaar zit
