# VLAM in-cluster ontsluiten: proxy-intern + ZAD-service

**Status**: fase 1 uitgerold en geverifieerd; fase 2 overgeslagen (besluit 2026-08-20, bewijs schuift naar de test na fase 3); fase 3 klaar voor de orch ship
**Datum**: 2026-08-20
**Context**: `vlam.md` (runbook VPN-opzet), `features/futures/vlam-api-vpn-proxy.md` (ontwerp), `~/IdeaProjects/vlam` (gebruikersdocumentatie en tooling)

## Wat we bouwen

Andere ZAD-projecten toegang geven tot de VLAM-API zonder VPN. De VPN-opzet (headscale, gateway, passthrough-proxy op 8080) bestaat voor laptops van buiten en blijft onaangeraakt. In-cluster afnemers krijgen een eigen pad: een nieuw component in het vlam-project dat plain HTTP aanneemt en zelf de TLS naar VLAM opzet, plus een dunne ZAD-service die de afnemer env-vars en een netwerkpad geeft.

```
afnemer-pod --http--> vlam-proxy-intern:8081 --https (SNI + Rijksdienst-CA-verificatie)--> vlam-api.rijksweb.nl (RON)
```

De gekozen smaak is "het werkt gewoon": de afnemer krijgt een `VLAM_API_URL` en hoeft geen CA te vertrouwen, geen SNI te kennen en geen hostAliases te hebben. Wie end-to-end TLS nodig heeft kan altijd nog naar de bestaande passthrough op 8080; dat pad bestaat al en is geen onderdeel van dit werk.

## Ontwerpbeslissingen (genomen in de voorbespreking)

1. **Eigen component, niet een tweede frontend op de bestaande proxy.** De VPN-proxy en de interne proxy krijgen verschillende wijzigingsritmes en gebruikersgroepen. Een eigen component (`vlam-proxy-intern`, naamvoorstel) vermijdt gedeelde herstarts, een gedeeld foutdomein (configfout raakt anders ook het VPN-pad) en gedeelde maxconn/geheugenlimieten. HAProxy is stateless, dus de kosten zijn één extra kleine pod.
2. **TLS-terminatie op de interne proxy, niet end-to-end.** Het CA-probleem (Rijksdienst Issuing CA2 zit in geen publieke bundel) wordt één keer opgelost op de proxy in plaats van per afnemer-runtime. Consequentie die vastgelegd moet worden: de interne proxy ziet dit verkeer in plaintext. Binnen ons eigen cluster en beheer, en de hop naar buiten is versleuteld en geverifieerd.
3. **Geen eigen certificaat op de interne hop.** Elk zelf uitgegeven certificaat verplaatst het vertrouwensprobleem naar de afnemer (en `SSL_CERT_FILE` vervangt in de meeste runtimes de hele bundel, wat andere HTTPS-calls breekt). Intern HTTP met netpols als toegangscontrole.
4. **Toegang: open op de poort, HERZIEN 2026-08-20.** Per-afnemer inbound-regels in `vlam-wt8` bleken onwerkbaar beheer (elke nieuwe afnemer een handmatige regel). Besluit: iedereen in het cluster mag netwerk-technisch bij `vlam-proxy-intern:8081`; de echte autorisatie zit bij VLAM zelf (per-gebruiker `VLAM_KEY`, zonder key geen completions). Daarvoor krijgt cross-domain-access een kleine uitbreiding: een **wildcard-peer** op inbound-regels: `from: { project: "*" }` betekent "geen projectlimiet" en rendert als een ingress-regel zónder from-selector op alleen die poort. `vlam-wt8` zet die regel één keer; daarna is afname van de vlam-service voldoende voor toegang. De SNI/vaste-upstream-begrenzing van de proxy blijft: open op de poort betekent alleen VLAM kunnen bereiken, niet RON.

## Aandachtspunten (benoemd, geen blokkades)

- **VLAM is een ZAD-project (`vlam-wt8`), geen platformdienst.** De ZAD-service wijst dus naar de namespace van een tenant-project. Dat is acceptabel en bewust. Zou VLAM later verhuizen naar een mailbox-achtige, platform-beheerde opzet, dan verandert alleen het endpoint waar de service naar wijst; het service-oppervlak (env-vars + netpol) blijft gelijk. De endpoint-configuratie moet daarom uit clusterconfiguratie komen, niet hardcoded in de service.
- **Dit is een cluster-specifieke oplossing.** VLAM bestaat alleen op `odcn-production` (RON-koppeling). De service moet op andere clusters afwezig of expliciet niet-afneembaar zijn. Mechanisme te bepalen in de ship (clusterconfig-gedreven beschikbaarheid; sleep-mode met zijn cluster-default is het bestaande precedent).
- **Herleidbaarheid richting SSC-ICT.** In-cluster afnemers zijn workloads, geen personen achter SSO. De vraag "accepteert SSC-ICT één bron-IP" uit het oorspronkelijke ontwerp wordt hiermee scherper. Vastleggen in de feature-doc; geen technische actie in deze ship.
- **De wijzigingen aan het vlam-project zelf gaan niet door deze repo.** Het projectbestand is `projects/vlam-wt8.yaml` in `github.com/RijksICTGilde/rig-cluster-projects` (de zad-projects-repo van productie, zie `GIT_PROJECTS_SERVER_URL` in de odcn-overlay); lokale checkout: `~/IdeaProjects/rig-cluster-test-git-repositories/rig-cluster-projects/projects/vlam-wt8.yaml`. De HAProxy-config zit daar als heredoc in de `command:` van het proxy-component; de attachments (`headscale-config`, `headscale-acl`, straks ook de Rijksdienst-CA als derde) staan AGE-versleuteld in datzelfde bestand, projectsleutel staat onder `config: age-public-key`. Aanpassen via portal of git, maar nooit allebei tegelijk (compare-and-swap-valkuil uit het runbook). De orch ship levert de ZAD-code en documentatie; fase 1 en 2 zijn wijzigingen in die repo plus het projectbestand van de testafnemer.
- **Feitjes uit het echte projectbestand**: het `namespace:`-veld zegt `vlam-wt8`, maar OPI prefixt dat op productie tot `rig-prd-vlam-wt8`; de service-URL is dus `http://productie-vlam-proxy-intern.rig-prd-vlam-wt8.svc.cluster.local:8081`. Het verouderde "nep-VLAM"-commentaar in `vlam-proxy` is bij fase 1 gecorrigeerd.

## Fase 1: `vlam-proxy-intern` in het vlam-project (ops, geen repo-code)

Nieuw component in `vlam-wt8`, naast de bestaande drie:

| | |
|---|---|
| image | `docker.io/library/haproxy:lts-alpine` (zelfde als bestaande proxy) |
| ports | inbound `[8081]`, outbound `[443]` |
| services | `attachments` (Rijksdienst-CA-keten als bijlage, mount als file) |
| probe | `scheme: http` op een `monitor-uri /healthz` in HAProxy |
| overig | `auto-tune-resources: false` (les uit het runbook), requests/limits vast, bijv. 64Mi/256Mi |

HAProxy-config via de `command`-heredoc (zelfde patroon als de bestaande proxy), kernpunten:

- frontend `mode http` op 8081, `monitor-uri /healthz`
- backend `server vlam vlam-api.rijksweb.nl:443 ssl sni str(vlam-api.rijksweb.nl) verify required ca-file /etc/haproxy/rijksdienst-ca.pem resolvers dns init-addr last,libc,none`
- `http-request set-header Host vlam-api.rijksweb.nl` richting upstream; binnenkomende Host-header bepaalt de bestemming dus niet
- de `resolvers`-sectie en `timeout server 10m` uit het runbook overnemen (HAProxy-DNS-les en taalmodel-timeouts)
- redirects worden teruggegeven, niet gevolgd (HAProxy-standaardgedrag, expliciet zo laten)
- `maxconn` expliciet zetten (les uit het runbook)

De RON-egress werkt automatisch mee: de `rig-ron`-annotatie staat op de namespace, niet op een component.

**Status: uitgerold en geverifieerd 2026-08-20.** Alle vier de deployments Healthy in ArgoCD, en vanuit een pod in `rig-prd-vlam-wt8` geeft `wget http://productie-vlam-proxy-intern:8081/v1/models` de volledige VLAM-modellenlijst, ook met een afwijkende Host-header (negatieve test geslaagd). Headscale herstartte eenmalig bij de reprocess (secret-herversleuteling + RWO-PVC met Recreate) en kwam gezond terug; het VPN-pad is verder onaangeraakt. Eerder gecontroleerd vóór de push: **Status: klaargezet 2026-08-20** in de lokale checkout van `rig-cluster-projects` (ongecommit). Gecontroleerd: schema-, service-config- en structuurvalidatie op de gemigreerde data (de structuurcheck strandt lokaal alleen op de subdomein-enforcer die een database wil; het subdomein wijzigt niet), het CA-attachment ontsleutelt via de master- en projectsleutel byte-voor-byte naar het origineel en de bestaande attachments blijven leesbaar, en `haproxy -c` keurt de config goed tegen het echte image. Let op bij uitvoeren: `ENABLE_GIT_MONITOR=false` op productie, dus na de git-push moet er expliciet een reprocess van `vlam-wt8` getriggerd worden (portal of API); doe dat meteen na de push en werk ondertussen niet in de portal aan dit project.

**Verify (de asserties van deze fase):**

1. Vanuit een pod in de vlam-namespace: `curl http://<unique-name>:8081/v1/models` geeft de VLAM-modellenlijst.
2. Negatief: een afwijkende `Host`-header verandert de bestemming niet (zelfde antwoord).
3. TLS-verificatie staat echt aan: met een opzettelijk verkeerde `ca-file` faalt de upstream-verbinding, met de juiste werkt hij. Eén keer aantonen, dan terugdraaien.
4. Het VPN-pad is onaangeraakt: `curl -v https://chat.rijksweb.nl/health` vanaf een laptop door de tunnel geeft nog steeds HTTP 200 met geldige certificaatvalidatie, en de bestaande proxy-pod is niet herstart.

## Fase 2: OVERGESLAGEN (besluit 2026-08-20)

De gebruiker kiest ervoor de service direct te bouwen; het cross-namespace-bewijs (twee projecten, poort 8081, positief én negatief) verhuist naar de acceptatietest van fase 3, met een testproject en eventueel de E2E-testpod als afnemer. De snippets hieronder blijven staan als referentie voor die test en voor het handmatig verlenen van toegang aan de VLAM-kant. De productie-OPI (image 2026.08.18) heeft cross-domain-access (sinds 2026-08-01), dus dit vergt geen uitrol.

In `vlam-wt8.yaml`, projectniveau `services:` (VLAM-kant is de toestemming):

```yaml
  - name: cross-domain-access
    schema-version: "1.0"
    config:
      inbound:
        - name: van-<afnemer-project>
          from: { project: <afnemer-project>, deployment: <afnemer-deployment>, component: <afnemer-component> }
          to: { component: vlam-proxy-intern, port: 8081 }
```

In het projectbestand van de testafnemer:

```yaml
  - name: cross-domain-access
    schema-version: "1.0"
    config:
      outbound:
        - name: naar-vlam
          from: { component: <afnemer-component> }
          to: { project: vlam-wt8, deployment: productie, component: vlam-proxy-intern, port: 8081 }
```

Plus op het afnemer-component een env-var `VLAM_API_URL=http://productie-vlam-proxy-intern.rig-prd-vlam-wt8.svc.cluster.local:8081`.

**Verify:**

1. Pod in het testproject bereikt `$VLAM_API_URL/v1/models` en krijgt de modellenlijst; een chat-completion met een bestaand model (`vlam-llm-medium-vast`) antwoordt.
2. Negatief: een pod in een project zónder regels krijgt een time-out op hetzelfde adres (geen foutmelding, een time-out, conform het netpol-gedrag uit het runbook).
3. Negatief: de VPN-passthrough op 8080 is vanuit het testproject níet bereikbaar (de toestemming geldt alleen poort 8081).

Slaagt fase 2, dan is het ontwerp bewezen en is al het volgende UX.

## Fase 3: ZAD-service (repo-code, de kern van de orch ship)

Een nieuwe service in `opi/services/catalog/`, werknaam **`vlam`** (naamvoorstel, te bevestigen; alternatief iets generiekers als dit later meer RON-diensten gaat dekken). Bewust dun:

- **Env-vars**: de service levert `VLAM_API_URL` aan elk component dat hem afneemt. De waarde komt uit clusterconfiguratie (endpoint per cluster), niet hardcoded. Injectiemechanisme uitzoeken in de ship: `ManifestContribution` kent `env_from_secrets` en `template_vars`; welk pad een niet-geheime platte env-var het schoonst levert is een implementatiekeuze, geen ontwerpvraag. Eventueel `VLAM_CHAT_URL` als tweede var, zie open punten.
- **Netpol**: de service contribueert de egress-regel aan afnemerskant zelf via `contribute_deployment_manifests` (het `cross-domain-access`-patroon, inclusief het prune-prefix-contract), met de peer uit dezelfde clusterconfiguratie. De ingress-kant blijft een expliciete `cross-domain-access` inbound-regel in `vlam-wt8`: de toestemming blijft bij de ontvanger en de service kan zichzelf geen toegang verlenen.
- **Beschikbaarheid**: alleen afneembaar op clusters waar de clusterconfig een VLAM-endpoint definieert. Hoe dat de wizard-kaart bereikt (kaart verbergen versus afname weigeren met uitleg) is een ontwerpkeuze binnen de ship; de validatie moet er in elk geval zijn, een kaart-filter alleen is geen validatie.
- **Binding en config**: deployment-binding (besluit gebruiker 2026-08-20, zoals cross-domain-access): een deployment neemt de service af, alle componenten van die deployment krijgen `VLAM_API_URL` en vallen onder de egress-regel. Geen configvelden in de eerste versie; er valt niets te kiezen. Wel een `help.md` die uitlegt wat het is, dat toegang door het VLAM-team verleend moet worden, en dat het verkeer intern onversleuteld tot de proxy loopt.
- **Ingress-kant (nieuw werkonderdeel van deze ship)**: breid cross-domain-access uit met een wildcard-peer op inbound-regels: `from: { project: "*" }` (vormbesluit gebruiker 2026-08-20, geen apart `open`-veld). Regels: `"*"` is uitsluitend geldig op `project` van een inbound-regel; `deployment`/`component` aan de peer-kant moeten dan leeg zijn (validatiefout, niet stil negeren, want "elk project maar alleen hun component X" is niet te bouwen); rendert als ingress-regel zonder from-selector, alleen de poort. NIET in de UI aanbieden: de project-optionsprovider biedt `*` niet aan, de regel is alleen via projectbestand/API te zetten; een bestaande wildcard-regel moet in de bewerk-flow wel zichtbaar blijven als "geen projectlimiet" (het bestaande principe dat een select een onbekende waarde niet stil laat vallen). Schema-fragment regenereren en `config_schema_version` bumpen, forward-only migratie zoals altijd. Vandaag geldt: een inbound-regel zonder complete peer wordt na de merge stil overgeslagen met een warning; de wildcard maakt daar een expliciete, gevalideerde vorm van. De egress-regel van de vlam-service selecteert de pods van de hele afnemer-deployment (label `deployment: <naam>`) richting namespace `rig-prd-vlam-wt8`, pod-labels `app: productie-vlam-proxy-intern` + `project: vlam-wt8`, poort 8081.
- Registratie volgens `instructions/services.md` (enum, definition, registry, schema-fragment indien config, guardrail-tests, golden manifests).

Na de eenmalige wildcard-inbound-regel in `vlam-wt8` is afname van de service voldoende: egress + env-var komen van de service, de poort staat aan de VLAM-kant al open. Geen per-afnemer beheer meer (herziene ontwerpbeslissing 4).

**Verify:**

1. Unit- en golden-manifest-tests: afname van de service produceert de env-var in de container-spec en de egress-netpol in de deployment-manifests; afmelden pruned beide.
2. Op een cluster zonder VLAM-endpoint in de config: afname wordt geweigerd met een begrijpelijke fout (test op het validatiepad, niet alleen op de kaart).
3. E2E in de sandbox met een dummy-endpoint in de sandbox-clusterconfig: service aanzetten via de wizard, env-var komt aan in de pod. (De echte upstream is niet testbaar in de sandbox.)
4. `uv run ruff check`, `ruff format`, `pyright`, en de service-guardrails uit `instructions/services.md`.

**Acceptatietest op productie (het overgeslagen fase 2-bewijs, ná de uitrol van de service):** de wildcard-inbound-regel in `vlam-wt8` zetten, de vlam-service aanzetten op een testproject (idee gebruiker: de E2E-testpod als afnemer), en dan: `$VLAM_API_URL/v1/models` geeft de modellenlijst vanuit de afnemer-pod; een project dat de service níet afneemt krijgt een time-out op hetzelfde adres (egress dicht, want de baseline staat alleen 80/443 toe); de VPN-passthrough op 8080 blijft vanuit de afnemer onbereikbaar (de wildcard-regel geldt alleen poort 8081).

## Fase 4: documentatie

1. **Feature-doc** `features/vlam-service.md` (of definitieve naam): wat het is, de tweesmaken-afweging (terminated hier, passthrough op 8080 blijft bestaan), de plaintext-kanttekening, het toegangsmodel (ontvanger beslist), de cluster-specificiteit, en de open herleidbaarheidsvraag richting SSC-ICT.
2. **`vlam.md` runbook** bijwerken: het nieuwe component, en dat er nu twee paden zijn (VPN voor mensen, service voor workloads).
3. **`~/IdeaProjects/vlam`**: korte sectie in de gebruikersdocumentatie dat in-cluster afnemers geen tunnel nodig hebben maar de ZAD-service.

## Open punten (beslissen tijdens of vóór de ship)

1. **Ook `chat.rijksweb.nl` ontsluiten?** Zo ja: tweede frontend op 8082 met eigen vaste backend (expliciet, geen Host-routering), en `VLAM_CHAT_URL`. Voorstel: alleen als er een concrete afnemer voor is, YAGNI.
2. **Servicenaam** en de kaarttekst richting gebruikers.
3. **CA-rotatie**: de CA-keten zit in een subPath-mount en wordt niet automatisch ververst; rotatie = bijlage vervangen + redeploy van alleen `vlam-proxy-intern`. Vastleggen in het runbook, en de vervaldatum van de keten noteren.
4. **Capaciteit**: de interne proxy deelt de RON-verbinding met de VPN-gebruikers bij VLAM zelf. Als workload-verkeer groot wordt is dat een gesprek met SSC-ICT over quota, niet iets dat wij met maxconn oplossen.
