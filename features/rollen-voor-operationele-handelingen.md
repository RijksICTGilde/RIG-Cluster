# Rollen voor operationele handelingen

Wie mag een job uitvoeren, een databaseconsole starten en een back-up terugzetten. Deze
drie handelingen liggen niet bij elk projectlid, maar bij de rollen die iets met het
project mogen DOEN.

## Wat het is

Het projectbestand kent vier rollen in `users`: `admin`, `owner`, `developer` en
`member`. Tot RC-323 bepaalde de rol alleen of je het project mocht BEWERKEN
(`PROJECT_EDIT_ROLES`, `admin` en `owner`); alle overige handelingen vroegen enkel
lidmaatschap, dus elke rol mocht ze. Sindsdien is er een tweede lijst:

- `PROJECT_EDIT_ROLES = ("admin", "owner")`: het projectbestand wijzigen.
- `PROJECT_OPERATE_ROLES = ("admin", "owner", "developer")`: operationele handelingen.

Beide staan in `opi/services/project_authorization.py`. Een platformbeheerder krijgt
altijd `admin`.

`member` is daarmee voor het eerst echt een LEESROL: meekijken, de status bekijken en een
back-up maken, maar niets starten dat code uitvoert of data overschrijft.

## Wie mag wat

| Handeling | admin | owner | developer | member |
|---|---|---|---|---|
| Het project bekijken, de status van een job of console bekijken | ja | ja | ja | ja |
| Een back-up aanmaken, de back-upstroom doorlopen | ja | ja | ja | ja |
| Een job starten en stoppen | ja | ja | ja | nee |
| Een databaseconsole starten en stoppen | ja | ja | ja | nee |
| Een back-up terugzetten | ja | ja | ja | nee |
| Het projectbestand bewerken | ja | ja | nee | nee |

### Waarom deze grens

Een job draait een vrij gekozen image met een vrij gekozen commando in de namespace van
het project, onder het project-serviceaccount, met het databasesecret van de deployment in
de omgeving (`opi/manager/job_manager.py`, `manifests/job-pod.yaml.jinja`). Een
databaseconsole geeft interactieve toegang tot die database. Terugzetten overschrijft de
data van een deployment, of schrijft met `restore_mode: new` een nieuwe deployment in het
projectbestand. Dat zijn speciale toegangsrechten in de zin van BIO2 8.02, en ze horen
niet bij een rol die bedoeld is om mee te kijken (least privilege, BIO2 5.18).

Een back-up MAKEN valt erbuiten: het leest en schrijft niets terug.

## Hoe het werkt

De grens zit aan de serverkant; de knop volgt hem.

| Waar | Poort |
|---|---|
| `POST /projects/{p}/jobs`, `/jobs/{sid}/stop` | `require_project_operate_access` |
| `POST /projects/{p}/db-console`, `/db-console/{sid}/stop` | `require_project_operate_access` |
| De modal en de statuspoll van beide (GET) | lidmaatschap |
| De modal-wizard `modal-restore` (init, stap, terug, skip, confirm, de modusknop) | `require_project_operate_access` |
| De modal-wizard `modal-backup` (dezelfde zes routes) | lidmaatschap |
| Elke andere modal-wizard-stroom | `require_project_edit_access` |

`require_project_operate_access` en `require_project_edit_access` staan in
`opi/web/project_edit_security.py` en delen één implementatie: ze verschillen alleen in de
rollenlijst en de foutmelding. `_require_flow_access` in `opi/web/router_detail_edit.py`
kiest de poort van een modal-wizard-stroom, zodat de zes routes van die wizard niet elk
hun eigen keuze maken. `_handle_backup_restore_submit` controleert bij `trigger_restore`
de rol opnieuw, als guard tegen een tussenstap die hem mist.

Voor de knoppen is er `may_operate_on_project` (hetzelfde bestand): een bool, geen poort.

- De dialogen voor job en console tonen een `member` een uitleg in plaats van het
  startformulier, en laten de stopknop weg.
- De knop "Herstellen" bij de back-upsnapshots verschijnt alleen voor wie mag terugzetten
  (`mag_terugzetten` in `_backup-snapshots.html.j2`).
- De knoppen "Databaseconsole" en "Job uitvoeren" staan in het actiesblok van de
  deploymentpagina, dat als geheel achter `admin`/`owner` zit. Een `member` ziet ze dus al
  niet, en het blok is NIET verruimd: dat zou `developer` de bewerkknoppen eromheen geven.

## Buiten deze grens

De API-kant heeft hem nog niet. Met een persoonlijk token kan een aanroeper via
`opi/api/restore_router.py` terugzetten; daar horen restore, jobs en db-console dezelfde
grens te krijgen. Dat hoort bij RC-322.

## Voor de volgende release-notitie

Eén regel, want dit neemt bestaande gebruikers iets af:

> **De rol "alleen lezen" kan geen job, databaseconsole of herstel meer starten.** Dat kan
> met de rol developer, owner of administrator. Een back-up maken en de status bekijken
> blijven voor elk projectlid.

## Afhankelijkheden

Geen. De rollen komen uit het projectbestand; de lijsten staan in
`opi/services/project_authorization.py`.
