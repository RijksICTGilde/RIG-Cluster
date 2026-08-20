# VLAM in-cluster ontsluiten: proxy-intern + ZAD-service

**Status**: fase 1 uitgerold en geverifieerd; fase 2 overgeslagen (besluit 2026-08-20, bewijs schuift naar de test na fase 3); fase 3 en 4 gebouwd in RC-142 (PR #138)
**Datum**: 2026-08-20
**Context**: `vlam.md` (runbook VPN-opzet), `features/futures/vlam-api-vpn-proxy.md` (ontwerp), `~/IdeaProjects/vlam` (gebruikersdocumentatie en tooling)

## Wat we bouwen

Andere ZAD-projecten toegang geven tot de VLAM-API zonder VPN. De VPN-opzet (headscale, gateway, passthrough-proxy op 8080) bestaat voor laptops van buiten en blijft onaangeraakt. In-cluster afnemers krijgen een eigen pad: een nieuw component in het vlam-project dat plain HTTP aanneemt en zelf de TLS naar VLAM opzet, plus een dunne ZAD-service die de afnemer env-vars en een netwerkpad geeft.

```
afnemer-pod --http--> vlam-proxy-intern:8081 --https (SNI + Rijksdienst-CA-verificatie)--> vlam-api.rijksweb.nl (RON)
```

De gekozen smaak is "het werkt gewoon": de afnemer krijgt een `VLAM_API_URL` en hoeft geen CA te vertrouwen, geen SNI te kennen en geen hostAliases te hebben. Wie end-to-end TLS nodig heeft kan altijd nog naar de bestaande passthrough op 8080.

## Ontwerpbeslissingen

1. **Eigen component, niet een tweede frontend op de bestaande proxy.** Verschillende wijzigingsritmes en gebruikersgroepen; een eigen component vermijdt gedeelde herstarts en een gedeeld foutdomein. HAProxy is stateless, dus de kosten zijn een extra kleine pod.
2. **TLS-terminatie op de interne proxy, niet end-to-end.** Het CA-probleem (Rijksdienst Issuing CA2 zit in geen publieke bundel) wordt één keer opgelost op de proxy in plaats van per afnemer-runtime. Consequentie: de interne proxy ziet dit verkeer in plaintext.
3. **Geen eigen certificaat op de interne hop.** Elk zelf uitgegeven certificaat verplaatst het vertrouwensprobleem naar de afnemer (en `SSL_CERT_FILE` vervangt in de meeste runtimes de hele bundel). Intern HTTP met netpols als toegangscontrole.
4. **HERZIEN 2026-08-20 (feedback tijdens de ship): GEEN toestemming per afnemer.** De oorspronkelijke beslissing was een `cross-domain-access` inbound-regel per afnemer, met de ontvanger als beslisser. Dat maakt de eigenaar van een gedeelde voorziening poortwachter van een zelfbedieningsplatform. In plaats daarvan zet `vlam-wt8` EENMALIG een inbound-regel zonder projectlimiet op `vlam-proxy-intern:8081`; daarna is afname van de `vlam`-dienst genoeg. Autorisatie zit bij VLAM zelf (API-sleutel).

## Fase 1: `vlam-proxy-intern` in het vlam-project (ops, geen repo-code)

**Uitgerold en geverifieerd 2026-08-20.** Component met `haproxy:lts-alpine`, inbound `[8081]`, outbound `[443]`, de Rijksdienst-CA-keten als attachment, `monitor-uri /healthz`, vaste resources. Backend met `ssl sni str(vlam-api.rijksweb.nl) verify required ca-file ...`, Host-header zelf gezet, `resolvers`-sectie, `timeout server 10m`, `maxconn` expliciet. Details in `vlam.md`, "Component 4".

**Verify (gedaan):** modellenlijst vanuit een pod in de vlam-namespace; een afwijkende Host-header verandert de bestemming niet; het VPN-pad onaangeraakt.

## Fase 2: OVERGESLAGEN (besluit 2026-08-20)

Het cross-namespace-bewijs verhuist naar de acceptatietest na fase 3.

## Fase 3: ZAD-service (repo-code, RC-142)

Nieuwe dienst `vlam` in `opi/services/catalog/vlam/`, bewust dun:

- **Env-var** `VLAM_API_URL`, uit clusterconfiguratie, via de nieuwe additieve `ManifestContribution.env_vars`.
- **Netpol**: de egress-regel aan afnemerskant via `contribute_deployment_manifests`, met het prune-prefix-contract.
- **Beschikbaarheid**: `Service.available_on_cluster` — de kaart verdwijnt EN het opslaan wordt geweigerd op een cluster zonder VLAM-endpoint.
- **Binding**: deployment-gebonden; geen configvelden; wel een `help.md`.
- **Wildcard-inbound in cross-domain-access** (feedback): `from: { project: '*' }` op een inbound-regel = geen projectlimiet, rendert als ingress zonder `from`-selector op alleen die poort. `deployment`/`component` moeten leeg zijn (validatiefout). Niet in de UI aangeboden; een opgeslagen wildcard wordt wel getoond. `config_schema_version` naar 1.1.

**Verify:** unit- en rendertoetsen, de weigering op een cluster zonder endpoint, een sandbox-e2e die de env-var in de DRAAIENDE pod meet, plus ruff/pyright en de service-guardrails.

**Acceptatietest op productie (na de uitrol):** de open regel in `vlam-wt8` zetten, de dienst op een testproject aanzetten, `$VLAM_API_URL/v1/models` ophalen vanuit de afnemer-pod, en controleren dat de VPN-passthrough op 8080 vanuit die afnemer onbereikbaar blijft.

## Fase 4: documentatie

1. `features/vlam-service.md` — wat het is, de tweesmaken-afweging, de plaintext-kanttekening, het toegangsmodel, de cluster-specificiteit, de herleidbaarheidsvraag richting SSC-ICT.
2. `vlam.md` — component 4 en de twee paden.
3. `~/IdeaProjects/vlam` — NIET gedaan: die checkout bestaat niet in de container van de ship.

## Open punten

1. **Ook `chat.rijksweb.nl` ontsluiten?** Alleen als er een concrete afnemer voor is (YAGNI). Niet gedaan.
2. **Servicenaam**: `vlam` / "VLAM-API". Iets generiekers is afgevallen: er is vandaag één RON-dienst.
3. **CA-rotatie**: de keten zit in een `subPath`-mount en wordt nooit vanzelf ververst; rotatie = bijlage vervangen plus een redeploy van alleen `vlam-proxy-intern`. Vastgelegd in `vlam.md`.
4. **Capaciteit**: de interne proxy deelt de RON-verbinding met de VPN-gebruikers; bij groei is dat een quotagesprek met SSC-ICT.
5. **Herleidbaarheid richting SSC-ICT**: in-cluster afnemers zijn workloads, geen personen achter SSO. Vastgelegd in de feature-doc; geen technische actie.
