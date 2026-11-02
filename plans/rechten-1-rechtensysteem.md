# Rechten, taak 1: het rechtensysteem

Opdracht: bouw één rechtensysteem voor API en portaal, met de rollen `viewer`, `developer`, `ops`, `admin`, `developer-ci` en `ops-ci`, een regelregister naar het model van Wies, een rolmatrix die uit dat register volgt, en een schemamigratie van `role` naar `roles`. Dit verandert gedrag.

**Lees eerst `plans/rechten-op-paden-tad-en-wies.md`.** Dat is het ontwerp en het contract; dit plan zegt alleen wat deze taak ervan bouwt en hoe je bewijst dat het klopt. Waar dit plan en het ontwerp elkaar tegenspreken, wint het ontwerp en meld je het in de PR.

Deze taak loopt parallel met taak 2 (`plans/rechten-2-audit.md`). Taak 3 (`plans/rechten-3-audit-koppeling.md`) bouwt op allebei.

## Waarom

ZAD beslist nu op twee manieren: de API met `Handeling` en `ROL_HANDELINGEN`, het portaal met rollijsten en ongeveer twintig losse `["admin", "owner"]`-controles. De rollen doen weinig (iedereen in productie is `admin`), een developer kan geen deployment wijzigen, en de team-wizard maakt elk nieuw lid admin zonder step-up. Ontwerp, paragraaf 2.

## Wat er moet gebeuren

De volgorde is de volgorde van de commits. Commit na elke stap; stap 2 moet groen zijn voordat stap 5 begint.

### 1. Woordenlijst naar het Engels

Ontwerp B10 en 8.1. `opi/core/handelingen.py` wordt `opi/core/authz/actions.py` met de enum `Action` en de decorator `@action`. `StepUpActie` wordt `StepUpAction`. `opi/api/principaal.py` wordt `opi/api/principal.py`. `Principaal`, `PrincipaalSoort`, `ROL_HANDELINGEN`, `LEESHANDELINGEN`, `MENS_VERPLICHT`, `SCHRIJFROLLEN`, `NietHerkend` en `ToegangGeweigerd` krijgen Engelse namen. De waarden komen uit de tabellen in ontwerp 8.1. Commentaar en docstrings blijven Nederlands.

Niet omzetten: Nederlandse namen buiten de authz-modules (`project_leden`, `LidSoort`, routepaden als `/leden`).

Werk de documenten bij die een `cause` of step-up-`action` noemen: `features/leden-en-api-tokens.md`, `features/otp-op-de-zad-gebruiker.md`, `features/projectsleutel-vervangen.md`, `features/platformbeheer-via-de-api.md`.

*Bewijs:* alle tests groen; `grep -rn "Handeling\|StepUpActie\|Principaal" opi tests` levert niets op.

### 2. Het register, met de huidige rechten

Bouw `opi/core/authz/engine.py`, `rules.py` en `matrix.py` uit ontwerp 5.3 en 5.4. De regels beschrijven in deze stap **de rechten van vandaag**, niet die van ontwerp paragraaf 4. Vertaal de huidige rol daarvoor tijdelijk: `admin` en `owner` naar `{admin, ops}`, `developer` naar de huidige operate-set, `member` naar `{viewer}`. Een platformbeheerder krijgt op elk project `{admin, ops}`. `ROL_HANDELINGEN` en `MENS_VERPLICHT` vervallen en volgen uit de regels.

Markeer elke leesactie in het register met `read=True`; dat vervangt `LEESHANDELINGEN`, en taak 3 gebruikt het om te bepalen wat in de audit komt.

Laat de API-handhaving in `opi/api/principal.py` `decide()` aanroepen.

*Bewijs:* een test die voor **elke** combinatie van (huidige rol, soort aanroeper, action, markering van de deployment, project met of zonder productie) de uitkomst van `decide()` vergelijkt met wat de code van vóór deze taak beslist, voor API en portaal. Leg de oude uitkomst vast door de huidige tuples en `ROL_HANDELINGEN` in de test over te nemen voordat je ze weghaalt. Een tweede test vergelijkt per route de step-up die `decide()` vraagt met het huidige `@requires_step_up`-predicaat. Beide zijn gelijk. Deze tests blijven bestaan tot stap 6 en worden daar vervangen door de matrixtest.

### 3. De matrixpagina

Ontwerp 5.4: een pagina voor platformbeheerders die de matrix uit het register toont, met `role_label` als kolomkop, een `s` bij een step-up, en een rij die zegt wat een service account kan. LOTC-componenten, zie `~/IdeaProjects/jinja-roos-components/ROOS_CLAUDE_REFERENCE.md` als die er is, anders de bestaande templates in `opi/templates_lotc/`. Een link ernaartoe vanaf de Team-kaart.

*Bewijs:* elke regel uit het register staat op de pagina; een regel zonder label, of met een scope buiten `SCOPES`, laat de opstart falen.

### 4. Elke route een action

Ook in `opi/web`. Nieuwe actions volgen `<object>.<werkwoord>`, Engels, kebab-case, en staan in de PR. Minstens nodig: `env-vars.manage`, `member.manage`, `environment.set`, `job.run`, `database.console`, `audit.read`. Platformroutes achter `is_platform_admin` krijgen een action onder `platform.*` en een regel met een eigen holder voor platformbeheerders; hun handhaving verandert niet.

Zet de opstartcontroles uit ontwerp 5.4 aan.

*Bewijs:* de applicatie start; een test bewijst dat een route zonder action, een action zonder regel en een CI-rol die niets kan elk de opstart laten falen.

### 5. Het portaal op `decide()`

Eén dependency die de action van de route leest en `decide()` aanroept (ontwerp 5.5). Hij vervangt `require_project_edit_access`, `_require_project_member_access`, `require_project_operate_access` en de losse rolcontroles in `opi/web/router.py`. Templates gebruiken `can(...)` in plaats van een rolnaam. `@requires_step_up` leest uit `decide()` of er een step-up nodig is; de mechaniek (dialoog, venster, replay, `X-ZAD-OTP`, `velden=`) blijft.

Zet bij elke beslissing de vier attributen uit ontwerp 8.3 op `request.state`, in API en portaal, ook bij een weigering. Taak 3 leest ze.

Geen schaduwfase: de test uit stap 2 dekt dit, want een beslissing is een pure functie van de invoer die hij doorloopt.

*Bewijs:* de test uit stap 2 blijft groen; `grep -rn '"owner"' opi` levert alleen nog de schemamigratie op; geen template noemt nog een rolsleutel; een test dat na een geweigerde en een toegestane aanroep alle vier de attributen uit 8.3 gezet zijn, voor een API- en een portaalroute.

### 6. De schemamigratie en de nieuwe regels

Schemamigratie naar 2.13 in `opi/services/schema_migration.py`, met een legacy-patch in `opi/schemas/project_legacy/`: `users[].role: x` wordt `users[].roles: [...]`. `admin` en `owner` worden `[admin, ops]`, `developer` blijft `[developer]`, `member` wordt `[viewer]`. Het schema accepteert alleen de zes rollen uit ontwerp paragraaf 4, als lijst met minstens één waarde. Een project heeft minstens één lid met `admin`. Valideer NA `migrate_to_latest()`, niet ervoor.

Vervang daarna de tijdelijke vertaling uit stap 2 door de regels van ontwerp paragraaf 4, met `ROLE_LABELS` (beginwaarde `developer` is `Ontwikkelaar`) en `ROLE_DESCRIPTIONS`. Dit is de gedragswijziging.

*Bewijs:*

- de migratie op een kopie van de projects-repo. Lokaal staat die in `~/IdeaProjects/rig-cluster-test-git-repositories/rig-cluster-projects-github`; kopieer hem naar een tijdelijke map en draai daar, nooit in het origineel, en push niets. Elk lid dat `admin` was heeft daarna `[admin, ops]`, nergens staat nog `role:`, elk bestand valideert. Is de repo er niet, maak dan een fixture met de vormen die het ontwerp in paragraaf 2 beschrijft en zeg in de PR dat de proef op de echte repo nog moet;
- een test die de gegenereerde matrix cel voor cel vergelijkt met de tabel in ontwerp paragraaf 4;
- de tests uit stap 2 die de oude rechten vastleggen gaan weg in deze commit; zeg in de PR welke en waarom.

### 7. Rollen schrijven

Ontwerp paragraaf 7 en B12. Eén servicefunctie (VOORSTEL `set_member_roles`) die zowel de team-wizard als de Team-kaart en `PUT .../leden/{naam}/rol` (neemt nu `roles: [...]`) aanroepen. Hij weigert een onbekende rol, een lege lijst en een wijziging die het project zonder `admin` laat. Tokens en service accounts blijven staan zoals `zet_verborgen_leden_terug` (`opi/forms/wizard/leden.py`) dat nu doet. Opslaan vraagt `member.manage` met step-up.

De team-wizard en de Team-kaart tonen per lid een meerkeuze met `role_label` en `ROLE_DESCRIPTIONS`; een nieuw lid heeft `[developer]` vooraf gekozen. Het formulier verstuurt de sleutel. `UserRoleOptionsProvider` (`opi/forms/visualizers/providers.py`, met de ongeldige rol `operator`) leest uit het register of verdwijnt. Een wizard-flow die `users` niet bezit en het toch indient, wordt geweigerd. De OTAP-markering zetten of verlagen vraagt `ops` met step-up.

*Bewijs:*

- een developer die de POST van de team-wizard of de rolroute stuurt krijgt 403 en het bestand verandert niet;
- een admin zonder verse step-up krijgt `step-up-required`;
- een onbekende rol, een lege lijst en het weghalen van de laatste admin worden geweigerd, via beide wegen;
- een developer kan de markering van een productiedeployment niet zetten of verlagen;
- een test die `ROLE_LABELS[Role.DEVELOPER]` wijzigt en ziet dat Team-kaart, wizard en matrix het nieuwe label tonen, terwijl het projectbestand `developer` houdt.

### 8. Documentatie

Een feature-document `features/rollen-en-rechten.md`: de rollen, de matrix (verwijs naar de pagina, kopieer hem niet), hoe je een regel toevoegt, hoe je een rol hernoemt op het scherm, en de spelregel voor een nieuwe rol. Werk `features/leden-en-api-tokens.md` bij (rollen als lijst, de tabel "wat een rol opent").

## Wat er buiten valt

- Alles van de audit: dat is taak 2 en 3. Raak `opi/migrations/` en `opi/services/audit.py` niet aan.
- Een mens toevoegen via de API, platformbeheer als grant met vervaltijd, veldrechten, `ApproverScope`. Ontwerp paragraaf 9.
- Nederlandse namen buiten de authz-modules omzetten.

## Wat niet mag

- Geen OPI uitrollen naar een cluster.
- Niets pushen naar de projects-repo, en de lokale checkout daarvan niet wijzigen.

## Verifieerbaar

1. `uv run ruff check . --fix && uv run ruff format . && uv run pyright` schoon in `operations-manager/python`.
2. De tests uit stap 4 tot en met 7 groen, plus de bestaande suite.
3. De matrixtest uit stap 6 is groen: de code doet wat ontwerp paragraaf 4 zegt.
4. De migratieproef uit stap 6: geen admin verliest rechten.
5. Ontwerp 8.3: de vier attributen staan op `request.state` na elke beslissing.
