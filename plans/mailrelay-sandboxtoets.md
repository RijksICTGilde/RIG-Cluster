# De identiteitstoets van de mailrelay draaien op de sandbox

**Prefix**: RC-114 (vervolg) — derde poging, na RC-138 en RC-139
**Basis**: `claude/mailrelay-ready`
**Doel**: één ding. Een bericht door de relay laten lopen en met een bestaand script
bewijzen dat de identiteitsregels doen wat ze beloven.

## Wat er al staat, en dus NIET opnieuw hoeft

Twee eerdere pogingen zijn omgevallen doordat hun worker-container verdween, maar hun werk
is in deze basis opgenomen. Alles hieronder is af:

- **De relay draait via een eigen ArgoCD Application** (`ron-infrastructure`, destination
  `rig-ron`, in `bootstrap/rig-system/kustomize/overlays/sandboxed-local`). Niet via de
  clusterkustomization: de CMP-plugin forceert daar `namespace: rig-system` op de hele
  build, wat de `rig-ron`-resources plat trok en stukliep op een dubbele
  `mail-relay-credentials` Secret.
- **De submission-listener draait op 2525 in de container**, met de Service op 587. Een
  niet-root proces krijgt op deze host EACCES op 25, 465 en 587. Het netwerkbeleid kijkt
  naar de podpoort en noemt dus ook 2525.
- **`auto-update = false` op het spamfilter.** Stalwart haalde bij elke start een regelset
  van GitHub; die route bestaat op de sandbox niet en de mislukte fetch gaf een
  `config.build`-fout.
- **De sink heeft een emptyDir op `/tmp`**, want Mailpit schrijft daar een sqlite-klad en
  het manifest had `readOnlyRootFilesystem`.
- **`task sandbox:create-sops-age-secret` maakt ook `rig-ron` en zijn sops-age-key aan**,
  want de CMP heeft beide nodig om te renderen en GitOps kan ze in namespaced mode niet
  zelf brengen.
- De mailrelay-overlay staat daarom bewust NIET in
  `infrastructure/bootstrap/clusters/sandboxed-local/kustomization.yaml`. Zet hem daar niet
  terug.

## Werk in kleine stappen en commit na elke stap

De workers van beide vorige pogingen verdwenen na acht tot vijftien minuten. Dat is een
omgevingsprobleem waar jij niets aan kunt doen, maar je kunt eromheen werken:

- **Commit na elke stap.** Een volgende dispatch begint dan waar jij ophield.
- **Zet niet zelf een sandbox op als er al een draait.** Kijk eerst met `kubectl get ns`.
  `task sandbox:setup` duurt vijf tot tien minuten en dat is de meeste tijd die je hebt.

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

**Schrijf geen uitkomst op die je niet gemeten hebt.** Een eerdere poging schreef in een
toelichting dat de regels waren doorgemeten terwijl geen van de genoemde bestanden was
aangeraakt. De assertie is de exitcode, niet een zin in een commit.

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

Let op dat dit geheim twee keer gerenderd wordt: in `rig-system` voor OPI, en in `rig-ron`
voor de relay via zijn eigen overlay.

**2. De database van de relay moet bestaan.** De relay bewaart zijn accounts en wachtrij in
PostgreSQL (`MAIL_DB_NAME: mailrelay`), niet op een PVC. Bestaat die database of die
gebruiker niet, dan start Stalwart niet op en zie je dat pas in de podlog.

## Stappen

1. **Claim de sandbox**: `orch sandbox claim`. Aan het eind `orch sandbox release`, ook als
   het misgaat.
2. **Het geheim maken** volgens punt 1 hierboven → verifieer: `sops --decrypt` op het
   resultaat toont de waarden. **Commit.**
3. **De overlay bouwen**:
   `SOPS_AGE_KEY="$(sed -n '3p' security/sandbox-key.txt)" kustomize build --enable-alpha-plugins --enable-exec --load-restrictor LoadRestrictionsNone infrastructure/bootstrap/infrastructure/mail/controller/overlays/sandboxed-local`
   → verifieer: bouwt zonder fout en bevat `rig-mail-relay`, `rig-mail-sink`, het geheim en
   het netwerkbeleid, allemaal in `rig-ron`.
4. **Synchroniseren**: `task sandbox:sync`, en wachten tot de Application
   `ron-infrastructure` gesynchroniseerd is → verifieer: `kubectl -n rig-ron get pods` toont
   `rig-mail-relay` en `rig-mail-sink` allebei Running. Start de relay niet op, lees dan
   zijn log; de eerste verdachte is de database uit punt 2. **Commit wat je moest aanpassen.**
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
   de toelichting bij in `infrastructure/bootstrap/clusters/sandboxed-local/kustomization.yaml`.
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
  naast de daglimiet, het besluit of het spamfilter aan gaat nu `auto-update` uit staat,
  `messages = 10` per sessie dat te laag is, en of een outbound throttle op `sender` het
  plusdeel meeneemt). Die horen bij een volgende taak. Kom je een gegeven tegen dat er iets
  over zegt, schrijf het op in het verslag maar verander niets.

## Verslag

De uitslag van de toets (alle vier de punten), het antwoord op de TLS-vraag uit stap 7, en
alles wat onderweg anders bleek dan hierboven beschreven. Dat laatste is het waardevolste
deel: de twee vorige pogingen leverden samen vijf blokkades op die niemand had voorzien.
