# BIO2-conformiteit: toegangsscheiding zonder VPN

**Status**: Compliance-notitie (risicoafweging vastgelegd)
**Norm**: BIO2 v1.3 definitief (9 januari 2026), gepubliceerd in de Staatscourant 5 maart 2026
**Scope**: ZAD / Operations Manager (OPI) op `odcn-production`
**Laatst herzien**: 2026-06-26

## Aanleiding en vraag

ZAD ontsluit applicaties en de selfservice-portal via SSO (Keycloak/OIDC) en bewust
*niet* via een VPN. De vraag is of dit toegestaan is onder de BIO2 en de NORA, of dat
een van beide een VPN dan wel een specifieke vorm van netwerk-toegangsscheiding
voorschrijft bij applicatie-ontwikkeling of deployment.

## Oordeel

**De BIO2 schrijft geen VPN voor.** Geen enkele control of overheidsmaatregel eist een
VPN. De BIO2 volgt de controlstructuur van NEN-EN-ISO/IEC 27002:2022 en is
technologie-neutraal en risicogestuurd: ze eist *aantoonbare, risicogestuurde
toegangsscheiding en beveiligde authenticatie*, niet een specifieke techniek. Een
identiteitsgerichte aanpak (SSO + MFA, mTLS/FSC, netwerksegmentatie via namespaces en
NetworkPolicies) is een volwaardige invulling van dezelfde controls.

SSO zonder VPN is BIO-conform, mits:

1. de keuze als expliciete risicoafweging is vastgelegd (deze notitie);
2. de beheer- en deploy-plane apart afgeschermd blijft (zie 8.20.02 hieronder);
3. segmentatie, sterke authenticatie en logging/monitoring de functie van een VPN
   compenseren.

De NORA-beveiligingsprincipes zijn afgeleid van de BIO en eveneens principe- en
risicogebaseerd. Ze schrijven geen VPN voor. Deze notitie baseert zich op de geladen
BIO2-tekst; de NORA-uitspraak is op basis van algemene kennis, niet op geladen
brontekst.

## Relevante BIO2-controls (verbatim) en hoe ZAD eraan voldoet

### 8.05 Beveiligde authenticatie (BBN 1)

> Er behoren beveiligde authenticatietechnologieen en -procedures te worden
> geimplementeerd op basis van beperkingen van de toegang tot informatie en het
> onderwerpspecifieke beleid inzake toegangsbeveiliging.

> **8.05.01** Voor het verlenen van toegang tot het netwerk aan externe leveranciers
> wordt vooraf een risicoafweging gemaakt. De risicoafweging bepaalt onder welke
> voorwaarden en voor hoelang de leveranciers toegang krijgen. Uit een registratie
> blijkt hoe de rechten zijn toegekend.

**Invulling**: gebruikers en ontwikkelaars authenticeren via Keycloak/OIDC met MFA. De
enige overheidsmaatregel bij 8.05 gaat over leveranciers-netwerktoegang, niet over
VPN voor eindgebruikers.

### 8.20 Beveiliging netwerkcomponenten (BBN 1)

> **8.20.02** Toegang tot beheerinterfaces van netwerkcomponenten zijn zo veel als
> mogelijk gescheiden van het gebruikersnetwerk.

**Invulling (belangrijkste aandachtspunt)**: dit raakt de *management plane*, niet de
eindgebruiker-flow. De ODCN-beheerpoort (8443) loopt via MetalLB
(`type: LoadBalancer`, `metallb.io/address-pool: public`, `externalTrafficPolicy:
Local`) en is afgeschermd met `haproxy.router.openshift.io/ip_whitelist` (standaard
VPN-only). ArgoCD-, OPI-admin- en kubectl-toegang horen achter die striktere grens. De
VPN-loze keuze geldt dus voor de *gebruiker*-flow, niet voor de beheer/deploy-plane.

### 8.21 Beveiliging van netwerkdiensten (BBN 1)

> **8.21.01** In koppelpunten met externe of onvertrouwde zones en vanwege
> netwerksegmentatie zijn maatregelen getroffen om mogelijke aanvallen die de
> beschikbaarheid van de informatievoorziening negatief beinvloeden te signaleren en
> te mitigeren.

> **8.21.04** Bij transport van gegevens over draadloze verbindingen (...) en bij
> bedrade verbindingen buiten het gecontroleerd gebied worden gegevens versleuteld
> (...).

**Invulling**: extern verkeer loopt over TLS (Route/IngressController). Voor
service-naar-service over onvertrouwde zones biedt FSC/mTLS de versleuteling en
wederzijdse authenticatie (PKIoverheid-certificaten op ODCN). Zie
`features/futures/fsc-mtls-attachments.md` en
`features/futures/fsc-mtls-klant-samenvatting.md`.

### 8.22 Netwerksegmentatie (BBN 1)

> Groepen informatiediensten, gebruikers en informatiesystemen behoren in de netwerken
> van de organisatie te worden gesegmenteerd.

> **8.22.01** Alle gescheiden groepen hebben een gedefinieerd beveiligingsniveau.

**Invulling**: per-project namespaces (`rig-prd-{project}`) met NetworkPolicies
realiseren de segmentatie; dit vervangt de isolatiefunctie die een VPN anders zou
leveren. Let op: op `sandboxed-local` (kindnet) handhaaft de CNI geen NetworkPolicies,
dus segmentatie is alleen op `odcn-production` daadwerkelijk afdwingbaar. Zie
`features/restrictive-network-policies.md`.

### 8.31 Scheiden van ontwikkel-, test- en productieomgevingen (BBN 1)

> Ontwikkel-, test- en productieomgevingen behoren te worden gescheiden en beveiligd.

> **8.31.01** In de productieomgeving wordt niet getest. Alleen met voorafgaande
> goedkeuring door de proceseigenaar kan hiervan worden afgeweken.

> **8.31.02** Significante wijzigingen in de productieomgeving worden altijd getest
> voordat zij in productie gebracht worden. (...)

**Invulling**: dit gaat over omgevingsscheiding, niet over netwerktoegang van
ontwikkelaars. De sandbox (`sandboxed-local`) staat los van productie
(`odcn-production`); deploys lopen via GitOps (ArgoCD) en niet via handmatige
prod-mutaties.

### 6.07 Werken op afstand (BBN 2)

> Wanneer personeel op afstand werkt, behoren er beveiligingsmaatregelen te worden
> geimplementeerd (...).

> **6.07.01** Geen overheidsmaatregel, zie inleiding deel 2 BIO-overheidsmaatregelen.

**Betekenis**: dit is de logische plek voor een VPN-eis, maar de BIO definieert hier
*geen* overheidsmaatregel en laat de invulling expliciet risicogestuurd over aan de
entiteit. Dit is het sterkste bewijs dat een VPN geen BIO-verplichting is.

## Compenserende maatregelen (de "VPN-functie" zonder VPN)

| VPN levert normaal | ZAD-equivalent | Control |
|---|---|---|
| Toegangscontrole tot de applicatie | SSO (Keycloak/OIDC) + MFA | 8.05 |
| Vertrouwelijkheid op transport | TLS extern, mTLS/FSC tussen services | 8.21.04 |
| Netwerk-isolatie tussen tenants | Namespaces + NetworkPolicies | 8.22 |
| Afscherming beheertoegang | VPN-only ip_whitelist op beheerpoort 8443 | 8.20.02 |
| Detectie/herleidbaarheid | Logging (8.15) + monitoring (8.16) | 8.15, 8.16 |

## Organisatorische borging (verantwoordelijkheid entiteit, niet de applicatie)

De BIO richt zich op de entiteit, niet op een enkele applicatie. ZAD kan compliance
hoogstens ondersteunen. De entiteit moet zelf borgen:

1. **Deze risicoafweging onderhouden en laten vaststellen** door de proceseigenaar/CISO.
   De BIO hangt vol formuleringen als "op basis van een expliciete risicoafweging"; een
   bewuste zero-trust/identity-first keuze is verdedigbaar, maar moet vastliggen
   (dreigingsmodel, compenserende maatregelen). Zonder dit papier wordt het bij een
   audit een bevinding wegens *ontbrekende afweging*, niet wegens een foute keuze.
2. **Beheer/deploy-plane gescheiden houden (8.20.02)** en die scheiding periodiek
   verifieren.
3. **Speciale bevoegdheden minimaal per kwartaal beoordelen** (8.02.01).

## Conclusie

Volgens BIO2 v1.3 is SSO zonder VPN conform, zolang de keuze is vastgelegd als
risicoafweging, de beheer/deploy-plane apart afgeschermd blijft, en segmentatie +
sterke authenticatie + logging/monitoring de VPN-functie compenseren. De BIO dwingt
geen VPN af; ze dwingt aantoonbare, risicogestuurde toegangsscheiding af, en die levert
ZAD zonder VPN.

## Disclaimer

Informele interne analyse, geen officieel BIO-product. De publicaties op
bio-overheid.nl zijn leidend. Controltitels/-teksten en doelen komen uit
NEN-EN-ISO/IEC 27002:2022 (in BIO-producten opgenomen met toestemming van NEN).
