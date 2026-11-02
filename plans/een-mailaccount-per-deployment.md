# Een mailaccount per deployment, met het wachtwoord in een secret

Elke deployment die mail verstuurt krijgt een eigen account op de relay, net zoals hij een eigen database-gebruiker heeft. Zo krijgt elke deployment ook een eigen verzendlimiet. Een testdeployment stuurt 100 berichten per dag zonder dat iemand iets hoeft goed te keuren. Productie stuurt er na goedkeuring 100.000. Het wachtwoord staat in het Kubernetes-secret van de deployment en niet meer in het projectbestand. Zo werken de database, MinIO en de andere services al, en accountgegevens horen nooit in het projectbestand.

**Basis**: forgejo `main`. Daar staan RC-399 (afzenderadres op aanvraag) en RC-400 (een limiter per account, goedkeuring op `messages-per-day`) al, samen met de string-schemaversies tot en met 2.13. Gebruik wat daar staat en bouw er geen tweede variant naast.

Alle nieuwe namen zijn een VOORSTEL, tenzij er een codeanker bij staat. Wijk je af, zeg dat dan in de PR. De regelnummers komen van forgejo main op `97f0bb9cc`.

Keycloak, met het realmblok met wachtwoord en TOTP in het projectbestand, valt buiten dit plan.

## Wat er nu staat

- **Een account per project per cluster.** `MailManager.create_resources_for_deployment` (`opi/manager/mail_manager.py:200`) gebruikt `generate_mail_account_name(project)` (`opi/utils/naming.py:699`), dat `project-<label>` oplevert. De docstring legt uit waarom er bewust niet per deployment is gekozen. Die reden vervalt met dit plan.
- **Het wachtwoord staat in het projectbestand.** `services[send-email].config.accounts` (`SendEmailAccount` in `opi/services/catalog/send_email/config_model.py`, schema `send-email.v1.0.json:116`) houdt per cluster vier velden bij: `username`, een AGE-versleuteld `password`, `from-address` en `bounce-address`.
  - Lezen gebeurt in `_existing_account_entry` (`mail_manager.py:643`) en in `_revoke` (r.575).
  - Schrijven gebeurt in `_store_account` (r.675) en in `_revoke`.
- **De deployment krijgt de gegevens al via een secret.** Dat is `SendEmailSecret` (`opi/utils/secrets.py:379`, `{deployment}-send-email`) met `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD` en `SMTP_FROM`. Het secret valt in de config-hash, dus een nieuw wachtwoord laat de pods herstarten.
- **Instellingen en goedkeuringen bestaan alleen op projectniveau.**
  - `config_api_fields`, `config_editables` en `config_approvals` (`send_email/__init__.py:304-376`) geven niets terug voor een andere laag dan PROJECT.
  - `config_value_approval` (`opi/services/catalog/approval.py:447`) bewaart zijn toestand in `services[<service>].config.<field>-approval` (`VALUE_APPROVAL_SUFFIX`, r.413). Daar staan de status, de gevraagde `value` en `approved-value`, dus alleen op het project.
  - Het item in de beheerpagina heeft `name` = de projectnaam en `domain` = de gevraagde waarde (r.541).
- **De relay leidt dingen af uit de accountnaam.** De envelope (`[session.mail] rewrite`) en de kopregel `X-ZAD-Project` (sieve `identiteitsregels`) halen `project-` van `authenticated_as` af. Zie `infrastructure/bootstrap/infrastructure/mail/controller/base/config.toml`, r.158-159, r.194 en r.257-259.
- **Er zitten twee gaten in het opruimen.**
  - Wie een hele deployment verwijdert (`delete_deployment` en `delete_deployment_from_yaml_change`), ruimt geen mailaccount op. Alleen wie de service uit een deployment haalt, doet dat (`handle_service_removal`, `mail_manager.py:486`).
  - `opi/jobs/service_orphan_sweep.py` kent mail helemaal niet.

## Wat er gebouwd wordt

### 1. Het account hoort bij de deployment

- **De naam komt uit `naming.py`.** Voorstel: een nieuwe functie naast `generate_mail_account_name`, bijvoorbeeld `generate_deployment_mail_account_name(project, deployment)`. Die geeft `project-<label>`. Het label is `<project>-<deployment>`, gevolgd door `-` en de eerste 6 hextekens van een sha256 over `<project>/<deployment>`. Het leesbare deel wordt zo ingekort dat het label binnen `MAIL_PROJECT_LABEL_MAX_LENGTH` (`naming.py:789`) blijft. Zo maakt elke aanroeper de naam op dezelfde manier. Bouw de naam nergens anders zelf op.
  - **Waarom er altijd een hash bij komt:**
    - Namen bevatten streepjes. Project `a-b` met deployment `c` en project `a` met deployment `b-c` krijgen zonder hash hetzelfde label.
    - Het huidige `mail_project_label` kapt gewoon af (`_truncate_if_needed`, `naming.py:451`). Twee lange namen kunnen dan samenvallen.
    - Met de hash kan een nieuwe naam niet samenvallen met een oud projectaccount `project-<project>`. Dat maakt de overgang (sectie 4) veilig.
  - **Zoek eerst** in `opi/utils/naming.py` en `opi/utils/` naar een bestaande helper die een naam met een hash inkort. Is die er, gebruik hem dan.
  - Het voorvoegsel `project-` blijft. Daardoor werkt de afleiding van de envelope op de relay ongewijzigd. Het plusdeel van het bounce-adres bevat dan project en deployment, zodat een bounce terug te leiden is tot de deployment.
- **`X-ZAD-Project` blijft de projectnaam.** Uit het label is het project niet meer te halen. Daarom schrijft OPI het project per account in het gegenereerde sieve-script `zad-afzenders`, naast `zad.afzender.naam` en `zad.afzender.adres` (`opi/connectors/mail.py:78-95`).
  - Voorstel: een sleutel `zad.project.<account>`, die `identiteitsregels` leest in plaats van de afgeleide waarde.
  - De `deleteheader` blijft buiten de if staan. Zo kan een applicatie geen eigen `X-ZAD-Project` meesturen.
  - Platformaccounts krijgen de kopregel nog steeds niet.
- **Het wachtwoord komt uit het secret**, volgens het patroon van de database (`database_manager.py`, helper `_get_existing_database_credentials_from_k8s` op r.1780):
  - **Het secret bestaat** (`{deployment}-send-email` in de namespace van de deployment): lees het wachtwoord via `SendEmailSecret.from_k8s_secret_data` en geef het aan `ensure_account`. Die zet het account zo nodig opnieuw op de relay (`update_principal`). Een relay die zijn database kwijt is, herstelt zich daarmee vanzelf.
  - **Het secret bestaat aantoonbaar niet**: genereer een nieuw wachtwoord (`generate_secure_password`).
  - **Het secret is niet te lezen**: stop met een fout en roteer NIETS.
    - Gebruik daarom `secret_exists` (`opi/connectors/kubectl.py:451`, die True, False of None geeft) en niet alleen `get_secret` (r.406). `get_secret` geeft bij elke kubectl-fout `None`.
    - `mail_manager._read_platform_secret` (r.355) doet dit al zo. Hergebruik dat in plaats van een tweede lezer te schrijven.
- **De accountgegevens gaan uit het projectbestand.** Weg gaan `accounts`, `SendEmailAccount`, `_existing_account_entry`, `_store_account`, `_entry_is_stale` en het schrijven in `_revoke`. Gebruikersnaam en adressen worden elke keer opnieuw afgeleid en nergens bewaard.

### 2. Instellingen op projectniveau, met een override per deployment

- `from-name`, `from-address` en `messages-per-day` staan op projectniveau en gelden voor elke deployment. Een deployment kan ze alle drie overschrijven.
- Dat gaat via het bestaande lagenmechanisme:
  - `ConfigLayer.DEPLOYMENT` (`opi/services/catalog/base.py:66`, pad `deployments[*]/services{svc}` op r.106);
  - de route `/config/deployment/{deployment_name}`;
  - `resolve_setting` (`config_settings.py:410`, met de volgorde deployment, dan project, dan default).
- Voorbeeld van een service die dit al zo doet: `postgresql_database/connection_limit.py`, met `layers=(PROJECT, DEPLOYMENT)` en `deployment_connection_limit`. Neem ook de schemavorm over: een config-model voor de deploymentlaag in `send_email/config_model.py` en een gegenereerd fragment `send-email.deployment.v1.0.json`, zoals `postgresql-database.deployment.v1.0.json`.
- Wat een deployment niet invult, erft hij van het project. Een deployment zonder eigen config is de normale situatie.

### 3. Goedkeuring: gebruik per project, afwijkende waarden per deployment

- **Of een project mail mag versturen** keurt een beheerder per project goed (`APPROVAL`, `__init__.py:95`). Dat blijft zo.
- **Een geërfde waarde vraagt niets.** Heeft het project een goedgekeurde 20.000 per dag of een goedgekeurd afzenderadres, dan geldt dat zonder nieuwe aanvraag voor elke deployment die erft.
- **Een override op een deployment boven de grens is een aanvraag voor die deployment**, wat de projectwaarde ook is:
  - **`messages-per-day`**: boven `MAIL_PROJECT_DEFAULT_MESSAGES_PER_DAY` (1000) is het een aanvraag. Tot en met 1000 kiest de deployment zelf.
    - Project 20.000, deployment vraagt 30.000: een aanvraag.
    - Project 20.000, deployment vraagt 5.000: ook een aanvraag, want 5.000 ligt boven de grens.
  - **`from-address`**: elke override is een aanvraag, net als op projectniveau (RC-399).
  - **`from-name`**: vrij, net als op projectniveau.
  - **Waarom los van het project**: de projectwaarde kan later omlaag. Een goedgekeurde override van een deployment blijft dan staan, omdat hij een eigen goedkeuring heeft en niet leunt op wat het project ooit had. Een deployment die erft, gaat wel mee omlaag.
- **Zolang een override niet is goedgekeurd**, geldt voor die deployment de laatst goedgekeurde override. Is die er niet, dan geldt de geërfde waarde. Een openstaande aanvraag verlaagt dus nooit wat er al gold. Dit is dezelfde regel als `effective_messages_per_day` (`__init__.py:240`) op projectniveau: de `approved-value` in het blok.
- **Hergebruik `config_value_approval`** (`approval.py:447`) en maak geen tweede fabriek. Voorstel: een parameter `layer` (PROJECT als default, of DEPLOYMENT) op dezelfde fabriek. Op de deploymentlaag gelden deze regels:
  - De toestand staat in `deployments[*].services[send-email].config.<field>-approval`, met dezelfde blokvorm (`status`, `value`, `approved-value`, `history`) en dezelfde regel bij een terugkeer naar een goedgekeurde waarde.
  - `_items` levert één item per deployment met een open of beoordeelde aanvraag.
  - `_record` en `_ensure_requested` werken per deployment.
  - `_notices` geeft de mededeling bij die deployment.
  - `config_approvals(ConfigLayer.DEPLOYMENT)` en `ensure_approval_requests` (`__init__.py:366-384`) leveren de aanvragen van de deploymentlaag.
- **De identiteit van een item moet de deployment bevatten.** De guard `validate_approval_decisions` paart items op (service, type, domain, name) (`opi/manager/project_validation.py:878`). De reseed in de beheerpagina (`opi/web/router_approvals.py:421`) doet dat op (type, domain, name).
  - Vragen twee deployments dezelfde waarde aan, of heeft een deployment dezelfde naam als het project, dan vallen hun items zonder aanpassing samen. Een goedkeuring kan dan meeliften op een ander item.
  - Voorstel: een item op de deploymentlaag krijgt `name` = `<project>/<deployment>`. Een `/` komt in geen van beide namen voor, dus dit valt nooit samen met een projectitem of met een andere deployment.
  - `_record` leest de deployment uit dat veld, of uit een apart veld `deployment` op het item, mits dat de reseed overleeft. Toets dat.
  - Neem in de beheerpagina de deployment op in het `subject`.

### 4. De overgang: een migratie, de rest bij de volgende verwerking

- **De schemastap is 2.14.** Een migratiestap haalt `accounts` uit elk send-email-blok, idempotent, naar het voorbeeld van `remove_domain_mode` (`opi/services/schema_migration.py:1104`). Daarbij hoort:
  - `"2.14"` in `SCHEMA_VERSIONS` (r.54) en een regel in `MIGRATION_STEPS` (r.1532). De module-check eronder eist dat die twee kloppen.
  - `x-zad-schema-version` in `opi/schemas/project_v2.json` naar `"2.14"`.
  - Een legacy-patch `opi/schemas/project_legacy/v2.13.json`, die `accounts` op 2.13 nog toestaat. Volg de vorm van `v2.12.json`, met een `$comment` dat uitlegt wat 2.14 veranderde.
  - Het fragment `send-email.v1.0.json`, opnieuw gegenereerd zonder `accounts`, of een nieuwe `config_schema_version`. Kies een van de twee en zeg in de PR waarom.
  - `tests/test_project_schema_versions.py` moet groen blijven. Die test controleert dat elke versie in de keten een schema heeft, en dat de oude vorm op 2.13 wordt geaccepteerd en op 2.14 geweigerd.
  - Versies zijn strings op forgejo main (`parse_schema_version`), dus "2.14" is geen float-probleem.
- **Bij de volgende verwerking** krijgt elke deployment met send-email een eigen account en een secret met nieuwe gegevens. De config-hash laat de pods herstarten, en die pakken het nieuwe account op.
- **Het oude projectaccount opruimen, zonder dat de mail onderbroken wordt.**
  - Het oude account (`project-<label>` zoals `generate_mail_account_name` het nu maakt) blijft op de relay staan zolang een live secret van een deployment van dat project het nog gebruikt.
  - Een verwerking die ziet dat geen enkel live `{deployment}-send-email` de oude gebruikersnaam nog heeft, verwijdert het account met `_delete_account` (`mail_manager.py:546`), inclusief de afzendersleutels en de limiter.
  - Is een secret niet te lezen, dan blijft het account staan. Zo valt er geen gat tussen het wegschrijven in git en de sync door ArgoCD.
- **Het centrale account wordt niet overgezet, maar vervangen.** Geen deployment neemt het oude projectaccount of zijn wachtwoord over: elke deployment krijgt een nieuw account met een nieuw wachtwoord in zijn secret, en het centrale account verdwijnt zodra het niet meer gebruikt wordt. Dit is een besluit van de product owner; bouw geen pad dat het oude account aan een deployment koppelt.
- **Het oude wachtwoord is niet meer nodig.** De migratie gooit het weg. Het oude account blijft werken met wat de pods nog in hun oude secret hebben, tot het is opgeruimd.

### 5. Opruimen

- **Deployment verwijderd**: het mailaccount van die deployment gaat mee. Voeg mail toe aan de service-stappen van `delete_deployment` en `delete_deployment_from_yaml_change` in `delete_project_manager`, naast Keycloak, Redis, de database en MinIO. Daarmee is ook het verwijderen van een project gedekt.
- **Service uit een deployment gehaald**: `delete_resources_for_deployment` (`mail_manager.py:504`) ruimt het account van die deployment op. De check of een andere deployment de service nog gebruikt (`_project_still_uses_send_email`, r.725) vervalt.
- **Goedkeuring ingetrokken**: `_revoke` verwijdert de accounts van alle deployments van het project en schrijft niets meer in het projectbestand.
- **Orphan-sweep**: `service_orphan_sweep` krijgt een inventaris van de mailaccounts.
  - Een account met het voorvoegsel `project-` is een wees als het niet hoort bij een live deployment met send-email. De verwachte namen komen uit alle projectbestanden, naar het patroon van `_classify_database` (`service_orphan_sweep.py:78`) en `_live_deployments` (r.65).
  - Platformaccounts vallen erbuiten (`_refuse_platform_account`).
  - De connector heeft hiervoor een lijst van principals nodig, en die ontbreekt nog in `opi/connectors/mail.py`.
  - Zo pakt de sweep ook een oud projectaccount op dat stap 4 om welke reden dan ook liet staan.
  - In de tenant mist de sweep rechten op een deel van de types. Hij moet een onvolledige inventaris als onvolledig melden, niet als leeg.

### 6. Wat het project ziet

- Het formulier en de API op deploymentniveau tonen `from-name`, `from-address` en `messages-per-day`. Als een veld leeg is, tonen ze de geërfde waarde. Ze tonen ook de stand van een aanvraag.
- `features/send-email.md`, met harde regelafbreking zoals de rest van het document:
  - Herschrijf de secties "Wat het platform beheert" (r.184) en "Het account hangt aan het project" (r.233-253): een account per deployment, het wachtwoord in het secret, override en goedkeuring per deployment.
  - Pas ook de tabel met `SMTP_USERNAME` (r.76) aan, en de passages over het plusdeel en `X-ZAD-Project` (r.98-133).
- Het totaal per project is nu het aantal deployments maal hun budget. Dat staat in het document; het is een bewuste keuze.

## De poort op de schrijfweg

Er komen twee nieuwe schrijfwegen bij: een override op deploymentniveau die de relay bereikt, en een wachtwoord dat uit een secret komt. Beide krijgen een poort:

- Naar `set_rate_limit` en `set_sender` gaat altijd de EFFECTIEVE waarde van die deployment (sectie 3), nooit de gevraagde. De poorten uit RC-400 (`TestDeBudgetpoortOpDeSchrijfwegNaarDeRelay`) en RC-399 (`TestDePoortOpDeSchrijfwegNaarDeRelay`) gelden per deployment.
- Een override op deploymentniveau die via de API binnenkomt, wordt gevalideerd tegen hetzelfde schema en dezelfde `RangeValidator` als op het project.
- Een besluit (`approved` of `denied`) op de deploymentlaag mag alleen via de goedkeuringsroute. De guard `validate_approval_decisions` moet ook die items zien. Toets dat een gewone schrijfactie het niet kan zetten.

## Toetsen

1. Twee deployments van een project krijgen twee accounts, elk met een eigen secret en een eigen limiter.
2. De naam komt uit `naming.py`. Hij is verschillend voor `a-b`/`c` en `a`/`b-c`, en blijft binnen 47 tekens bij een projectnaam van 30 en een deploymentnaam van 63 tekens.
3. Een tweede verwerking zonder wijziging leest het wachtwoord uit het secret en roteert niets. Er gaat geen schrijfactie naar de relay.
4. Een secret dat aantoonbaar ontbreekt, geeft een nieuw wachtwoord. Een secret dat niet te lezen is, geeft een fout en geen rotatie.
5. Het projectbestand bevat na verwerking geen `accounts` en geen wachtwoord.
6. Migratie 2.14:
   - haalt `accounts` weg;
   - is idempotent;
   - levert een resultaat dat valideert tegen het schema;
   - een 2.13-bestand met `accounts` valideert op 2.13 en wordt op 2.14 geweigerd.
7. Een deployment zonder eigen config erft `from-name`, `from-address` en `messages-per-day` van het project, ook een goedgekeurde 20.000, zonder aanvraag.
8. Een override boven 1000 is een aanvraag op die deployment, ook als het project een hogere goedgekeurde waarde heeft. Tot de goedkeuring krijgt de relay de geërfde waarde.
9. Een override van `from-address` is een aanvraag op die deployment. Een override van `from-name` niet.
10. Twee deployments die dezelfde waarde aanvragen, geven twee items. De goedkeuring van het ene item keurt het andere niet goed. Een deployment met dezelfde naam als het project valt niet samen met het projectitem.
11. Een gewone schrijfactie kan een aanvraag op de deploymentlaag niet op `approved` of `denied` zetten.
12. `X-ZAD-Project` bevat de projectnaam, ook bij een lang en ingekort label. Een applicatie kan de kopregel niet zelf zetten.
13. Het oude projectaccount blijft staan zolang een live secret het noemt. Het wordt opgeruimd zodra geen enkel secret het nog noemt. Een onleesbaar secret laat het staan.
14. Een deployment verwijderen ruimt zijn account, afzendersleutels en limiter op de relay op.
15. De orphan-sweep meldt een account zonder live deployment en laat platformaccounts met rust.
16. Bestaande tests die op het projectaccount leunen, gaan om naar het nieuwe model en worden niet zonder vervanger weggegooid. Het gaat om:
    - `TestTheAccountBlockIsPlatformData`, `TestHetOpgeschrevenAccountVeroudertNiet` en `TestTheAccountIsSharedByTheProject` in `tests/test_send_email_service.py`;
    - `accounts` in `tests/test_v2_router.py:355`;
    - het mailaccountblok in `tests/test_schrijvers_tegen_het_schema.py:605`.

## Uitrol

Via forgejo main, daarna mee in de volgende PR van forgejo main naar GitHub main. Daarna verwerkt OPI elk project met send-email opnieuw (reprocess), en draait de migratie bij het laden. Controleer na de uitrol op de relay dat er per deployment een account is en dat de oude projectaccounts verdwijnen.

## Buiten de scope

- Keycloak, en elk ander blok met gegevens in het projectbestand.
- Een totaal per project boven de budgetten per deployment.
