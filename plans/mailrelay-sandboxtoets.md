# De mailrelay draaien in de sandbox en de identiteitsregels bewijzen

**Prefix**: RC-114 (vervolg) — tweede poging, na RC-138
**Basis**: `claude/mailrelay-ready`
**Doel**: de relay en zijn testupstream draaiend krijgen op de sandbox, en met een bestaand
script bewijzen dat de identiteitsregels doen wat ze beloven.

## Wat er al staat, en dus NIET opnieuw hoeft

RC-138 is vier keer omgevallen doordat zijn worker-container verdween, maar het werk dat hij
wél afmaakte is in deze basis opgenomen:

- de mailrelay-overlay staat **aan** in `infrastructure/bootstrap/clusters/sandboxed-local/kustomization.yaml`
- `MAIL_RELAY_API_URL` staat **aan** in de OPI-deployment van datzelfde clustertype

Begin dus niet daar. Eén ding uit die commit is wél rechtgezet en dat is leerzaam: er stond
in een toelichting geschreven dat de identiteitsregels waren doorgemeten en dat de uitslag in
`docs/ron-koppeling.md` stond, terwijl geen van beide bestanden was aangeraakt. **Schrijf
geen uitkomst op die je niet hebt gemeten.** De assertie hieronder is de exitcode van een
script, niet een zin in een commit.

## Werk in kleine stappen en commit na elke stap

De worker van de vorige poging verdween telkens na drie tot elf minuten. Dat is een
omgevingsprobleem waar jij niets aan kunt doen, maar je kunt er wel omheen werken:

- **Commit na elke stap hieronder.** Een volgende dispatch begint dan waar jij ophield in
  plaats van bij nul.
- **Zet niet zelf een sandbox op als het even kan.** `task sandbox:setup` duurt vijf tot tien
  minuten en dat is langer dan de vorige workers leefden. Kijk eerst met
  `kubectl get ns` of er al een draait. Is die er niet en lukt het opzetten niet binnen jouw
  levensduur, commit dan wat je hebt en meld dat in het verslag.

## De assertie

`operations-manager/python/scripts/mail_identity_check.py` eindigt met exitcode 0. Dat is de
hele opdracht. Het script stuurt drie berichten met een expres foute `From:` en een expres
foute envelope, en toetst:

1. de `From:` is overschreven met `noreply-rijksapp@rijksoverheid.nl`, met behoud van de
   weergavenaam waar de applicatie er een zette
2. ook zonder weergavenaam, en ook bij een kaal adres zonder punthaken
3. de envelope is `noreply-rijksapp+<account>@rijksoverheid.nl` en blijft in hetzelfde
   domein als de `From:` (de belangrijkste: `rijksoverheid.nl` publiceert `p=reject` en wij
   ondertekenen niet met DKIM, dus SPF-uitlijning is het enige dat een bericht door DMARC
   krijgt)
4. de `Received`-keten en de verklikkerheaders zijn eraf

## Twee dingen die je zonder waarschuwing verkeerd doet

**1. Het relay-geheim bestaat voor geen enkel cluster, en je mag NIET alles regenereren.**
`infrastructure/bootstrap/infrastructure/secrets/config/overlays/sandboxed-local/mail-relay-secret.yaml.sops.yaml`
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

**2. De database van de relay moet bestaan.** De relay bewaart zijn accounts en wachtrij in
PostgreSQL (`MAIL_DB_NAME: mailrelay`), niet op een PVC. Bestaat die database of die
gebruiker niet, dan start Stalwart niet op en zie je dat pas in de podlog.

## Stappen

1. **Claim de sandbox**: `orch sandbox claim`. Aan het eind `orch sandbox release`, ook als
   het misgaat.
2. **Het geheim maken** volgens punt 1 hierboven → verifieer: `sops --decrypt` op het
   resultaat toont de vier waarden. **Commit.**
3. **De build controleren**:
   `SOPS_AGE_KEY="$(sed -n '3p' security/sandbox-key.txt)" kustomize build --enable-alpha-plugins --enable-exec --load-restrictor LoadRestrictionsNone infrastructure/bootstrap/clusters/sandboxed-local`
   → verifieer: bouwt zonder fout en bevat `rig-mail-relay`, `rig-mail-sink` en het
   netwerkbeleid. Faalt hij op iets anders dan het geheim, dan is dat een bevinding.
4. **Synchroniseren**: `task sandbox:sync`, en wachten tot ArgoCD de namespace `rig-ron`
   heeft aangemaakt → verifieer: `kubectl -n rig-ron get pods` toont `rig-mail-relay` en
   `rig-mail-sink` allebei Running. Start de relay niet op, lees dan zijn log: dat is
   vrijwel zeker de database uit punt 2. **Commit wat je onderweg moest aanpassen.**
5. **Een account**: OPI maakt bij het opstarten zijn eigen platformaccount aan
   (`ensure_platform_mail_account`) en bewaart het wachtwoord in de Secret uit
   `MAIL_PLATFORM_SECRET_NAME` in de namespace van OPI → verifieer: die Secret bestaat en de
   log zegt dat het account klaarstaat. Lukt dat niet, maak dan een projectaccount door een
   testproject de dienst `send-email` te geven en het goed te keuren.
6. **De toets draaien**:
   ```bash
   kubectl -n rig-ron port-forward svc/rig-mail-relay 1587:587 &
   kubectl -n rig-ron port-forward svc/rig-mail-sink 8025:8025 &
   cd operations-manager/python
   uv run python scripts/mail_identity_check.py --user <account> --password <geheim>
   ```
   → verifieer: exitcode 0 en drie regels met het vaste adres en het juiste plusdeel.
7. **De TLS-vraag beantwoorden.** De sink biedt geen STARTTLS aan, terwijl de relay
   `[remote.upstream.tls]` op STARTTLS met strikte certificaatcontrole heeft staan. Komt er
   post aan, dan valt Stalwart dus terug op platte tekst en is `allow-invalid-certs = false`
   een voorkeur; komt er niets aan, dan is het een garantie. **Schrijf de uitkomst op in
   `docs/ron-koppeling.md`.**
8. **De markering weghalen.** Slaagt de toets, haal dan het blok "NOG NIET GEMETEN" weg bij
   identiteitsregel 2 in
   `infrastructure/bootstrap/infrastructure/mail/controller/base/configmap.yaml` en zet
   ervoor in de plaats wat er gemeten is, in dezelfde stijl als de regels eromheen. Werk ook
   de toelichting bij in `infrastructure/bootstrap/clusters/sandboxed-local/kustomization.yaml`,
   want daar staat nu dat de toets nog niet gedraaid is.
9. `orch sandbox release`.

## Wat niet de bedoeling is

- **Niets naar productie.** Deze taak raakt `sandboxed-local` en verder niets. De
  odcn-overlay blijft uitgecommentarieerd.
- **Geen bestaande geheimen regenereren**, zie punt 1.
- **De identiteitsregels niet aanpassen om de toets te laten slagen.** Faalt de toets, dan is
  dat de uitkomst en hoort hij in het verslag: welk van de vier punten faalde, met de
  werkelijke `From:`, envelope en headers erbij. Een regel bijbuigen tot het groen is, maakt
  de toets waardeloos.
- Het script niet verbouwen behalve als het zelf stuk is; het is de assertie.
- **Blijf van de limieten af.** Er staan vier bekende vervolgpunten open (een burst-limiter
  naast de daglimiet, het besluit of het spamfilter aan gaat, `messages = 10` per sessie dat
  te laag is, en of een outbound throttle op `sender` het plusdeel meeneemt). Die horen bij
  een volgende taak, niet bij deze. Kom je onderweg een gegeven tegen dat er iets over zegt,
  schrijf het op in het verslag maar verander niets.

## Verslag

Wat er in de PR moet staan: de uitslag van de toets (alle vier de punten), het antwoord op de
TLS-vraag uit stap 7, en alles wat onderweg anders bleek dan hierboven beschreven. Dat laatste
is het waardevolste deel, want dit plan is geschreven zonder dat er ooit een relay heeft
gedraaid.
