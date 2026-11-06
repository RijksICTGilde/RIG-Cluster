# De ASVS-meetlat

Een afvinklijst die per eis uit de OWASP Application Security Verification Standard zegt of
een automatische toets hem dekt, welk plan hem bouwt, of dat hij handwerk blijft.

Waarom: een externe audit loopt tegen een methodiek. Zonder eigen afvinklijst weten we niet
wat gedekt is en wat niet, en komt elk gat als bevinding terug in plaats van als een keuze
die we al hadden gemaakt.

## De drie bestanden

| Bestand | Wat het is |
|---|---|
| `architecture/asvs-5.0-eisen.yaml` | de meetlat: de 253 ASVS 5.0-eisen op L1 en L2, letterlijk uit de standaard. Wordt niet met de hand bewerkt |
| `architecture/asvs-zelfbeoordeling.yaml` | ons oordeel per eis. Dit is het bestand dat je bijwerkt |
| `docs/security/asvs-zelfbeoordeling.md` | het afvinkdocument, gegenereerd. Nooit met de hand bewerken |

De eisen staan apart van het oordeel, zodat de toets echt iets kan bewijzen: elke eis uit de
standaard moet een oordeel hebben, en een oordeel over een eis die niet bestaat valt op.

```bash
task asvs-afvinklijst   # schrijf docs/security/asvs-zelfbeoordeling.md opnieuw
task asvs-check         # faalt bij een gat of bij drift op het document
```

`task asvs-check` draait in CI (`ci.yml`, job `asvs-meetlat`) en is ook
`tests/test_asvs_zelfbeoordeling.py`.

## De vier statussen

| status | betekent | moet erbij |
|---|---|---|
| `toets` | een automatische toets dekt de eis | `toets:`, een of meer paden die bestaan |
| `plan` | een goedgekeurd plan bouwt de dekking | `plan:`, het RC-nummer |
| `handwerk` | mensenwerk, geen CI | `eigenaar:`, wie het doet |
| `nvt` | de eis geldt hier niet | `toelichting:`, waarom niet |

Een verwijzing bij `toets` is een pad (`operations-manager/python/tests/test_csrf_enforcement.py`)
of een pad met een job erachter (`.github/workflows/security.yml#sast-semgrep`). De checker
eist dat het bestand bestaat, en bij een job ook dat die job in die workflow staat. Dat is
het verschil tussen dekking en een belofte: verdwijnt een testbestand of wordt een job
hernoemd, dan gaat de meetlat rood in plaats van stil onwaar te worden.

## Hoe de verdeling er nu uitziet

Op 21 oktober 2026, niveau L2: 122 eisen met een automatische toets, 6 via een plan, 83
handwerk, 42 niet van toepassing. De volledige tabel staat in
`docs/security/asvs-zelfbeoordeling.md`.

Waar het handwerk zit, is geen verrassing en wel informatief:

- **V6 Authentication en V10 OAuth/OIDC** vullen samen 40 van de 83. ZAD is relying party:
  wachtwoordbeleid, MFA, de levensduur van een authorization code en het gedrag van de
  OpenID Provider zijn Keycloak-configuratie, niet ZAD-code. Die regels hebben
  platformbeheer als eigenaar.
- **V16 Logging** heeft de verzameling buiten het cluster nog niet.
- De **nvt**-regels zijn techniek die hier niet bestaat: WebRTC (7), GraphQL, SAML, LDAP,
  XPath, LaTeX, JNDI, memcache, en de keten rond lookup secrets en OTP's.

## Een regel bijwerken

1. Pas de regel aan in `architecture/asvs-zelfbeoordeling.yaml`.
2. `task asvs-afvinklijst` en commit het document mee.
3. `task asvs-check` moet groen zijn.

Een nieuwe toets gebouwd? Zet de eis op `toets` met het pad erbij. Een eis van `plan` naar
`toets` schuiven hoort bij de PR die de toets oplevert.

## Naar een nieuwe ASVS-versie

De eisen komen uit de officiele JSON van de standaard:

```bash
curl -sSL -o /tmp/asvs.json \
  https://raw.githubusercontent.com/OWASP/ASVS/master/5.0/docs_en/OWASP_Application_Security_Verification_Standard_5.0.0_en.json
```

Daaruit volgen `id`, `niveau`, `kapittel`, `sectie` en de eistekst van elke eis met `L` 1 of
2. Na het bijwerken meldt `task asvs-check` welke nieuwe eisen nog geen oordeel hebben, en
welke oordelen over een eis gaan die niet meer bestaat. De standaard staat onder CC BY-SA
3.0; de bronvermelding staat in de kop van het eisenbestand en in het document.
