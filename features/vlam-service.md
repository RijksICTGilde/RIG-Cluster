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

## Aanzetten opent het pad NIET

Dit is het deel dat het vaakst wordt overgeslagen. De dienst regelt jouw kant: je pods mogen naar
buiten, naar de proxy. De andere kant is een toestemming in het projectbestand van `vlam-wt8`, en
die geeft het VLAM-team:

```yaml
  - name: cross-domain-access
    schema-version: "1.0"
    config:
      inbound:
        - name: van-<jouw-project>
          from: { project: <jouw-project>, deployment: <jouw-deployment>, component: <jouw-component> }
          to: { component: vlam-proxy-intern, port: 8081 }
```

Dat is met opzet zo: de ONTVANGER bepaalt wie er binnen mag. Een afnemer die zichzelf toegang kan
verlenen heeft geen toestemming maar een formaliteit. Het is hetzelfde model als
`cross-domain-access`, en het gebruikt letterlijk dezelfde dienst.

Zolang die regel er niet is, krijgt je applicatie geen foutmelding maar een verbinding die blijft
hangen tot hij afloopt. Zo werken netwerkregels; het is geen storing.

Let op de asymmetrie: de dienst is deployment-breed (elk component krijgt het adres en valt onder de
uitgaande regel), maar een inbound-regel benoemt EEN component. Wie meerdere componenten echt naar
VLAM laat praten, vraagt per component een regel aan.

## Twee smaken, en waarom deze zo is

Er zijn twee paden naar VLAM, en ze bestaan naast elkaar:

| | VPN-pad (poort 8080) | deze dienst (poort 8081) |
|---|---|---|
| voor | mensen op een laptop | workloads in het cluster |
| TLS | end-to-end, niet getermineerd | getermineerd op de proxy |
| CA-probleem | lost de gebruiker zelf op | een keer opgelost, op de proxy |
| toegang | Keycloak-login met rolfilter | netwerkregel, per afnemer |

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
