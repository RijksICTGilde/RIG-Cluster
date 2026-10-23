# Een projectsleutel vervangen

Elk project heeft een eigen AGE-sleutelpaar (`config.age-public-key`, `config.age-private-key`). Dat paar versleutelt de geheimen van het project: `config.api-key`, Keycloak-credentials, TOTP-zaad, `user-env-vars`, aliassen, helm-values en bijlagen in het projectbestand, plus alles wat OPI als `*.sops.yaml` naar `zad-deployments` schrijft. In elke namespace staat de sleutel als Secret `sops-age-key`, waar de CMP hem bij het renderen ophaalt.

Die sleutel is zichtbaar voor elke `admin` en `owner` van het project. Vertrekt zo iemand, dan moet de sleutel vervangen kunnen worden. BIO2 v1.3 control 8.24 vraagt ook periodieke vervanging. Deze route doet allebei. De rotatie van de PLATFORMsleutel staat apart: zie `features/sops-sleutel-roteren.md`.

## De route

```bash
curl -X POST "https://zad.sandbox.rijksapp.dev/api/v2/projects/demo/:rotate-project-key" \
  -H "Authorization: Bearer $ZAD_TOKEN" \
  -H "X-ZAD-OTP: 123456"
```

Alleen met het token van een platformbeheerder, plus een verse code uit zijn authenticator: een projectlid kan zijn eigen sleutel niet vervangen. Zonder token `401`, met het token van een gewone gebruiker `403`, zonder code `403` met `type: urn:zad:step-up-required` en `action: projectsleutel-vervangen`. Zie `features/platformbeheer-via-de-api.md`. Het antwoord is `202` met een `task_id`; de voortgang staat op `/api/tasks/{task_id}`.

Het resultaat van de taak:

| veld | wat het zegt |
|---|---|
| `public_key` | de nieuwe publieke sleutel van het project |
| `rotated_fields` | de veldpaden die naar de nieuwe sleutel zijn omgezet, bijvoorbeeld `components[0].user-env-vars` |
| `skipped_fields` | de versleutelde velden die niet met de oude projectsleutel opengingen. Normaal zijn dat de velden op de platformsleutel (`config.age-private-key`, `repositories[].password`). Staat er een ander pad bij, dan ging dat veld met geen enkele sleutel open en was het al onleesbaar vóór de rotatie |
| `backup_namespaces` | de namespaces waarvan het Kopia-wachtwoord is meeverhuisd |
| `processing` | hoe het verwerken van het project daarna afliep |

Veldpaden, nooit waarden: het resultaat is leesbaar voor wie de taak polt.

## Wat de taak doet

1. Een nieuw sleutelpaar maken met `generate_sops_key_pair`.
2. De omzetting en de opslag in één `ProjectStore.mutate`: de store leest de verse staat uit git, de omzetting loopt daarop, en het resultaat wordt gevalideerd en gecommit. De commitboodschap noemt het aantal omgezette velden.
   - Elk veld dat met de OUDE projectsleutel opengaat gaat naar de nieuwe publieke sleutel, met dezelfde plaintext en dezelfde opslagvorm (een gepantserd AGE-blok blijft een blok, `base64+age:` blijft `base64+age:`). Dat is `rotate_project_key` in `opi/utils/key_rotation.py`, die dezelfde lus gebruikt als de platformrotatie in `scripts/`.
   - Het nieuwe paar komt in `config`: de publieke helft plat, de private helft ingepakt met `wrap_project_key`, net als bij projectaanmaak. Dat is de project-KEK zodra die er is, en de platformsleutel als terugval (RC-334, `features/sops-sleutel-roteren.md`). De oude helft wordt met `unwrap_project_key` gelezen, dus ook een projectbestand dat nog onder de platformsleutel staat gaat mee.
3. Het Kopia-wachtwoord van elke namespace van het project meeverhuizen (zie hieronder).
4. Het project verwerken, langs dezelfde weg als `:refresh`.

`mutate` en niet `save`: wordt het projectbestand tijdens de rotatie door iemand anders geschreven, dan draait de omzetting opnieuw op wat er landde, dus gaat een gelijktijdig toegevoegd versleuteld veld ook mee. Een drieweg-merge zou zo'n veld schoon toepassen terwijl `config.age-public-key` al de nieuwe sleutel draagt, en geen validatie toetst voor WELKE recipient een waarde versleuteld is: het zou dus stil op de vervangen sleutel blijven staan.

Gaat één veld niet mee, dan wordt de hele omzetting afgebroken en is er niets geschreven: de functie werkt op een kopie en controleert daarna opnieuw of nog een veld met de oude sleutel opengaat.

## De backups moeten mee

Het wachtwoord van de Kopia-repository van een namespace is geen opgeslagen geheim maar een afleiding uit het Secret `sops-age-key` (`derive_backup_password`, `opi/manager/backup/base.py`).
Vervang je de projectsleutel zonder meer, dan verandert die afleiding mee en opent de bestaande
repository niet meer. De backup van die namespace staat dan stil: de pod stopt met exitcode
`BACKUP_REPOSITORY_UNUSABLE_EXIT_CODE` en laat de opslag ongemoeid.

Daarom zet de taak het wachtwoord van de repository zelf om (`kopia repository
change-password`), per namespace van het project op deze cluster. Bestaande snapshots blijven
daarmee leesbaar met het wachtwoord dat uit de NIEUWE sleutel volgt. Een namespace zonder
repository levert geen fout op: daar valt niets over te zetten.

Gemeten tegen kopia 0.23.1: `change-password` raakt geen snapshots. Snapshots van vóór de wissel
zijn na de wissel nog leesbaar en terug te zetten; het oude wachtwoord opent de repository niet meer.

Die wissel staat NA een geslaagde opslag en VOOR het verwerken, en daar is maar één plek voor.
Na de opslag, omdat de wissel de enige stap buiten git is: faalt hij, dan zijn beide
wachtwoorden nog af te leiden (het oude uit de sleutel in de git-historie van het
projectbestand, het nieuwe uit het opgeslagen bestand) en is de reparatie `kopia repository
change-password` met de hand. Zou de wissel vóór de opslag staan, dan hangt het nieuwe
wachtwoord aan een sleutel die alleen in het geheugen bestaat: een mislukte opslag, of een pod
die precies dan herstart, laat een repository achter die met geen enkel wachtwoord meer opengaat
en die je dus ook niet met de hand kunt repareren. En voor het verwerken, omdat het verwerken de
nieuwe sleutel als Secret `sops-age-key` in de namespace zet, en het oude wachtwoord juist uit
dat Secret wordt afgeleid.

Faalt de wissel, dan staat het projectbestand al op de nieuwe sleutel en de repository nog op het
oude wachtwoord; de taak meldt die stap en verwerkt het project niet. De backup van die namespace
staat dan stil tot de wissel met de hand is gedaan (of tot het projectbestand is teruggezet).

Een backup-pod wist een prefix nooit: gaat de repository niet open, dan stopt hij met een fout
in plaats van met leeg geruimde opslag (RC-339). Een vergeten omzetting kost dus geen snapshots,
alleen een stilstaande backup.

## Wie opnieuw moet inloggen

Een secret dat zijn waarde uit de VORIGE ciphertext overneemt kan die met de nieuwe sleutel niet
meer lezen en krijgt een verse waarde. Dat raakt `keep_existing_values`, het cookie-secret van de
authorization wall: na de rotatie zijn alle lopende sessies achter die login ongeldig en moet
iedereen die erachter zat één keer opnieuw inloggen. Plan de rotatie daarop.

## Wat de taak niet doet

- **De platte waarden onder de sleutel vervangen.** Het database- en bucketwachtwoord, de Keycloak-credentials en de api-key blijven wat ze zijn; alleen de sleutel waarmee ze versleuteld staan verandert. Wie de waarden zelf wil vervangen doet dat per dienst.
- **Een knop in de UI.** De route is er voor een platformbeheerder.

## Het resultaat controleren

```bash
# de nieuwe publieke sleutel in het projectbestand
grep age-public-key <clone van zad-projects>/projects/demo.yaml

# de recipients van de bestanden in zad-deployments: alleen de nieuwe sleutel
sops filestatus <clone van zad-deployments>/demo/<deployment>/secrets.sops.yaml

# de sleutel in elke namespace van het project
kubectl get secret sops-age-key -n rig-demo -o jsonpath='{.data.key}' | base64 -d | grep 'public key'

# en de applicaties van het project: demo-project en demo-<deployment>
# (-A, want welke namespace de ArgoCD-instantie heeft verschilt per cluster)
kubectl get applications -A | grep demo-
```

Het verwerken zet het namespace-Secret eerst en pusht daarna de nieuwe `*.sops.yaml` (`process_project`: namespaces, dan SOPS-secrets, dan de manifesten). Tussen die twee staat een kort venster waarin de namespace de nieuwe sleutel heeft en de deploymentrepo nog de oude bestanden, dus waarin een render van de CMP faalt; de volgende sync leest de nieuwe bestanden en wordt groen. De volgorde zelf komt uit de code.

Het venster duurt ongeveer twintig seconden (sandbox-meting). Na tien seconden staat de nieuwe sleutel in het namespace-Secret terwijl zad-deployments nog de OUDE recipient noemt. Na twintig seconden (taak-einde) noemde dat bestand alleen de nieuwe recipient en waren beide Applications (`<project>-project`, `<project>-productie`) `Synced / Healthy` op de rotatie-commit.

Het volgen van de taak gaat met hetzelfde token: `/api/tasks/{task_id}` wil de api-key van het project zelf of het bearertoken van wie de taak startte, en dat laatste is hier het e-mailadres van de platformbeheerder in `created_by`. Die route vraagt geen `X-ZAD-OTP`; alleen de rotatie zelf doet dat.

## Terugzetten

Het oude sleutelpaar staat in de git-historie van het projectbestand in `zad-projects`. Terugzetten is dus een revert van de commit die de taak maakte (`Projectsleutel van <project> vervangen (<n> velden omgezet)`), gevolgd door `POST /api/v2/projects/{project_name}/:refresh`. De verwerking zet het namespace-Secret en de bestanden in `zad-deployments` dan terug op de oude sleutel, want die weg kijkt naar wat het projectbestand zegt.

Het backupwachtwoord gaat niet mee met zo'n revert: dat staat in de repository zelf, niet in git. Wie terugzet draait ook `kopia repository change-password` terug, met het wachtwoord dat uit de oude sleutel volgt.

Faalt de taak halverwege, dan noemt hij de stap waarin het misging. Is het projectbestand al opgeslagen en strandde het verwerken, dan is de nieuwe sleutel de waarheid en is een `:refresh` de manier om het verwerken opnieuw te proberen.

## Afhankelijkheden

- `age-keygen` in de OPI-image, voor `generate_sops_key_pair`.
- De project-KEK (`PROJECT_KEK_FILE`) om de huidige projectsleutel te openen en de nieuwe in te pakken, met de platformsleutel (`SOPS_AGE_PRIVATE_KEY`) als terugval zolang niet elk projectbestand is omgezet. Draait de rotatie met een KEK, dan staat de nieuwe projectsleutel onder de KEK en opent de platformsleutel hem niet meer.
- Een platformbeheerder met een geldig SSO-token; zie `features/platformbeheer-via-de-api.md`.
- `kopia` in de OPI-image, voor het meeverhuizen van het backupwachtwoord.
