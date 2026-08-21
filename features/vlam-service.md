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
| een uitgaande netwerkregel | van de pods van je deployment naar precies die ene proxy-pod, op poort 8081 |

Er zijn geen instellingen. Er valt niets te kiezen: een endpoint, een variabele, een regel.

Verwacht je bibliotheek een andere naam dan `VLAM_API_URL`, gebruik dan een alias op je component.

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

## Twee smaken, en waarom deze zo is

Er zijn twee paden naar VLAM, en ze bestaan naast elkaar:

| | VPN-pad (poort 8080) | deze dienst (poort 8081) |
|---|---|---|
| voor | mensen op een laptop | workloads in het cluster |
| TLS | end-to-end, niet getermineerd | getermineerd op de proxy |
| CA-probleem | lost de gebruiker zelf op | een keer opgelost, op de proxy |
| toegang | Keycloak-login met rolfilter | netwerkregel + de API-sleutel van VLAM |

De keuze voor terminatie is de kern van deze dienst. Het certificaat van `vlam-api.rijksweb.nl` komt
van `Rijksdienst Issuing CA2` en zit in geen enkele publieke bundel, dus zonder terminatie moet elke
afnemer die keten in zijn eigen runtime vertrouwen. Dat is per taal anders, en in de meeste runtimes
VERVANGT `SSL_CERT_FILE` de hele bundel, waarmee je andere HTTPS-verkeer van diezelfde applicatie
sloopt. Eén keer goed op de proxy is beter dan tien keer bijna goed bij de afnemers.

**De prijs, expliciet:** tussen de afnemer-pod en de proxy is het verkeer niet versleuteld. Dat
blijft binnen ons cluster en ons beheer, en de hop naar buiten is wel versleuteld en geverifieerd.
Wie versleuteling tot aan VLAM zelf nodig heeft, gebruikt het VPN-pad; dat blijft bestaan.

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
| netwerkregel | `contribute_deployment_manifests`, één `NetworkPolicy` per deployment, egress-only, `podSelector` op `deployment` + `project` |
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
- **`chat.rijksweb.nl` ontsluiten.** Kan (tweede frontend op 8082 met een eigen vaste backend en een
  `VLAM_CHAT_URL`), maar pas als er een concrete afnemer voor is.

## Zie ook

- `vlam.md` — het runbook van de gateway en van `vlam-proxy-intern` zelf
- `features/futures/vlam-api-vpn-proxy.md` — het ontwerp achter het VPN-pad
- `instructions/services.md` — hoe een dienst in elkaar zit
