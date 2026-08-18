# De mailrelay draaien in de sandbox en de identiteitsregels bewijzen

**Prefix**: RC-114 (vervolg)
**Basis**: `claude/mailrelay-ready`
**Doel**: de relay en zijn testupstream draaiend krijgen op de sandbox, en met een bestaand
script bewijzen dat de identiteitsregels doen wat ze beloven.

## Waarom dit een taak is en geen handeling

De dienst `send-email` is af, gemerged met de basis, en ruff, pyright en 9323 tests zijn
schoon. Wat ontbreekt is het enige dat er werkelijk toe doet: **er heeft nog nooit een
bericht doorheen gelopen.** De regels waarop het hele ontwerp rust zijn regels in een
sieve-script, en een sieve-script dat stilletjes niets doet ziet er precies zo uit als een
script dat werkt. De rest van de relayconfiguratie is destijds nagespeeld tegen een
draaiende Stalwart; het overschrijven van de `From:` in zijn huidige vorm niet.

## De assertie

`operations-manager/python/scripts/mail_identity_check.py` eindigt met exitcode 0. Dat is de
hele opdracht. Het script stuurt drie berichten met een expres foute `From:` en een expres
foute envelope, en toetst:

1. de `From:` is overschreven met `noreply-rijksapp@rijksoverheid.nl`, met behoud van de
   weergavenaam waar de applicatie er een zette
2. ook zonder weergavenaam, en ook bij een kaal adres zonder punthaken
3. de envelope is `noreply-rijksapp+<account>@rijksoverheid.nl` en blijft in hetzelfde
   domein als de `From:` (dit is de belangrijkste: `rijksoverheid.nl` publiceert
   `p=reject` en wij ondertekenen niet met DKIM, dus SPF-uitlijning is het enige dat een
   bericht door DMARC krijgt)
4. de `Received`-keten en de verklikkerheaders zijn eraf

## Drie dingen die je zonder waarschuwing verkeerd doet

**1. Het relay-secret bestaat voor geen enkel cluster, en je mag NIET alles regenereren.**
`infrastructure/bootstrap/infrastructure/secrets/config/overlays/<cluster>/mail-relay-secret.yaml.sops.yaml`
ontbreekt. `task generate-secrets-for-cluster` stopt met `exit 0` zodra er één `.sops.yaml`
in die map staat, en de enige manier om dat te omzeilen is elk bestaand geheim van dat
cluster weggooien en opnieuw genereren. Dat roteert Keycloak, MinIO, PostgreSQL en pgadmin
mee. **Doe dat niet.** Maak dit ene geheim met de hand: neem het sjabloon
`infrastructure/bootstrap/infrastructure/secrets/templates/mail-relay-secret.yaml`, vul de
waarden voor `sandboxed-local` in en versleutel het met `task encrypt-secret`. De waarden:

- `MAIL_UPSTREAM_HOST`: `rig-mail-sink.rig-ron.svc.cluster.local` (de sink, niet rijksweb)
- `MAIL_DB_HOST`: `rig-db-rw.rig-system.svc.cluster.local` (op de sandbox draait de
  database in `rig-system`, niet in `rig-prd-operations`)
- `MAIL_RELAY_ADMIN_PASSWORD` en `MAIL_DB_PASSWORD`: zelf een wachtwoord kiezen; het
  `@secret-gen:random:24`-merkteken wordt alleen door de generatietaak ingevuld en blijft
  bij handmatig versleutelen letterlijk staan

**2. De overlay staat uitgecommentarieerd.** In
`infrastructure/bootstrap/clusters/sandboxed-local/kustomization.yaml` staat de regel
`#  - ../../infrastructure/mail/controller/overlays/sandboxed-local` met een toelichting
erboven waarom. Die reden (bereikbaarheid van de upstream niet bewezen) is vervallen, dus
die regel gaat aan en de toelichting wordt bijgewerkt.

**3. De database van de relay moet bestaan.** De relay bewaart zijn accounts en wachtrij in
PostgreSQL (`MAIL_DB_NAME: mailrelay`), niet op een PVC. Bestaat die database of die
gebruiker niet, dan start Stalwart niet op en zie je dat pas in de podlog.

## Stappen

1. **Claim de sandbox**: `orch sandbox claim`. Aan het eind `orch sandbox release`, ook als
   het misgaat.
2. **Sandbox opzetten**: `task sandbox:setup` (~5 tot 10 minuten). Bestaat er al een, dan
   is dat prima; sla deze stap dan over.
3. **Het geheim maken** volgens punt 1 hierboven → verifieer: `sops --decrypt` op het
   resultaat toont de vier waarden.
4. **De overlay aanzetten** volgens punt 2 → verifieer:
   `SOPS_AGE_KEY="$(sed -n '3p' security/sandbox-key.txt)" kustomize build --enable-alpha-plugins --enable-exec --load-restrictor LoadRestrictionsNone infrastructure/bootstrap/clusters/sandboxed-local`
   bouwt zonder fout en bevat de relay, de sink en het netwerkbeleid.
5. **Synchroniseren**: `task sandbox:sync`, en wachten tot ArgoCD de namespace `rig-ron`
   heeft aangemaakt → verifieer: `kubectl -n rig-ron get pods` toont `rig-mail-relay` en
   `rig-mail-sink` allebei Running.
6. **Een account maken.** OPI maakt bij het opstarten zijn eigen platformaccount aan
   (`ensure_platform_mail_account`) en bewaart het wachtwoord in de Secret uit
   `MAIL_PLATFORM_SECRET_NAME` in de namespace van OPI → verifieer: die Secret bestaat en
   de log zegt dat het account klaarstaat. Lukt dat niet, maak dan een projectaccount door
   een testproject de dienst `send-email` te geven en het goed te keuren.
7. **De toets draaien**:
   ```bash
   kubectl -n rig-ron port-forward svc/rig-mail-relay 1587:587 &
   kubectl -n rig-ron port-forward svc/rig-mail-sink 8025:8025 &
   cd operations-manager/python
   uv run python scripts/mail_identity_check.py --user <account> --password <geheim>
   ```
   → verifieer: exitcode 0 en drie regels met het vaste adres en het juiste plusdeel.
8. **De TLS-vraag beantwoorden.** De sink biedt geen STARTTLS aan, terwijl de relay
   `[remote.upstream.tls]` op STARTTLS met strikte certificaatcontrole heeft staan. Komt er
   post aan, dan valt Stalwart dus terug op platte tekst en is `allow-invalid-certs = false`
   een voorkeur; komt er niets aan, dan is het een garantie. **Schrijf de uitkomst op in
   `docs/ron-koppeling.md`**, want dat verschil bepaalt of er op productie een tweede slot
   op zit.
9. **De markering weghalen.** Slaagt de toets, haal dan het blok "NOG NIET GEMETEN" weg bij
   identiteitsregel 2 in
   `infrastructure/bootstrap/infrastructure/mail/controller/base/configmap.yaml` en zet
   ervoor in de plaats wat er gemeten is, in dezelfde stijl als de regels eromheen.
10. `orch sandbox release`.

## Wat niet de bedoeling is

- **Niets naar productie.** Deze taak raakt `sandboxed-local` en verder niets. De
  odcn-overlay blijft uitgecommentarieerd.
- **Geen bestaande geheimen regenereren**, zie punt 1.
- **De identiteitsregels niet aanpassen om de toets te laten slagen.** Faalt de toets, dan
  is dat de uitkomst en hoort hij in het verslag: welk van de vier punten faalde, met de
  werkelijke `From:`, envelope en headers erbij. Een regel bijbuigen tot het groen is, maakt
  de toets waardeloos.
- Het script niet verbouwen behalve als het zelf stuk is; het is de assertie.

## Verslag

Wat er in de PR moet staan: de uitslag van de toets (alle vier de punten), het antwoord op
de TLS-vraag uit stap 8, en alles wat onderweg anders bleek dan hierboven beschreven. Dat
laatste is het waardevolste deel, want dit plan is geschreven zonder dat er ooit een relay
heeft gedraaid.
