# Implementatieplan: invites als echte service, met portaal-UI

**Eindproduct van dit document:** `plans/invite-service.md`, de opdracht voor de sessie die dit bouwt.
**Eindproduct van de implementatie:** een `invite`-servicepackage onder `opi/services/catalog/`, een werkende beheer-UI in het portaal, een migratie van vier bestaande projecten, `features/invites.md` volgens de projectconventie, en als sluitstuk een ingevulde controle tegen `instructions/service-review-checklist.md` (taak 14).

**Status: alle beslissingen zijn genomen op 1 augustus 2026 (sectie 3 en 7). Dit document is daarmee een contract, geen voorstel.** Wijkt de bouwer ergens van af, dan is dat een terugkoppeling waard en geen eigen keuze.

Alle paden in dit document zijn relatief aan `operations-manager/python/` tenzij ze met `instructions/`, `features/`, `plans/` of `opi/schemas/` beginnen. Regelnummers verwijzen naar de stand van branch `branches-samenvoegen-naar-main` op het moment van schrijven; controleer ze voordat je een bestand aanpast, ze schuiven.

---

## 1. Huidige situatie

Uitnodigingen bestaan al en werken, maar ze zitten volledig buiten het servicesysteem en er is geen enkele manier om er een aan te maken behalve met de hand YAML bewerken.

**Waar het nu staat.** `invites:` is een top-level sectie in het projectbestand, naast `services:`, `components:` en `deployments:`. Het schema kent het als `opi/schemas/project_v2.json:55` (`"invites": { "$ref": "#/$defs/invites" }`), met `$defs/invites` op regel 707 tot 723 (alleen `settings.default_language` en `active[]`) en `$defs/invite` op regel 725 tot 745. `$defs/i18n-text` (regel 747) wordt uitsluitend door `$defs/invite` gebruikt. De root van het schema heeft `additionalProperties: false` (regel 8), dus alles wat niet in `$defs/invite` staat maakt het hele projectbestand ongeldig.

**Wat er aan code is.** Lezen gebeurt in `opi/handlers/project_file_handler.py:2925` tot `3107`: `extract_invites_config`, `get_invite_settings`, `get_invite_by_key`, `get_all_active_invites`, `get_invite_auth_methods`, `get_invite_message`, `get_invite_success_title`, `get_invite_success_button`. De inwisselstroom zit in `opi/manager/invite_manager.py` (647 regels) en `opi/api/invite_routes.py` (1034 regels, acht routehandlers op zeven paden: landing, sso, idp, sso/callback, register GET, register POST, success, error). De router wordt gemount in `opi/server.py:478`. De vier publieke sjablonen zijn `opi/templates/invite-landing.html.j2`, `invite-register.html.j2`, `invite-success.html.j2` en `invite-error.html.j2`.

**Wat er niet is.** Er is geen `ServiceType`-waarde voor invites, geen package onder `opi/services/catalog/`, geen configmodel, geen editables, geen visualizers, geen formuliersectie, geen blok op de detailpagina, en geen enkele route die een invite aanmaakt, wijzigt of verwijdert. Alle acht bestaande routes zijn inwisselroutes voor de uitgenodigde gebruiker. Een invite ontstaat alleen doordat iemand met schrijfrechten handmatig YAML in het projectbestand zet. Dat is de kern van het gat.

**Hoe het er in productie uitziet.** Vier projecten gebruiken invites: `asses-k2n.yaml`, `dp-bn7.yaml`, `mb-docs-helmfile.yaml` en `openp-4pw.yaml` in `/Users/robbertuittenbroek/IdeaProjects/rig-cluster-test-git-repositories/rig-cluster-projects-github/projects`. Alle vier hebben exact dezelfde vorm: `settings.default_language: nl`, één invite in `active`, met `key`, `realm_roles: [allowed-user]`, `application_url`, `contact_email`, en de drie i18n-blokken `message`, `success_title` en `success_button` met `nl` en `en`. De sleutels zijn zelfgekozen en goed raadbaar: `invulhulpen`, `welcome-to-desa-portfolio`, `welcome-to-docs`, `welcome-to-openproject`.

**Waarom dit nodig is.** Drie redenen, in volgorde van gewicht. Ten eerste: een projectbeheerder kan geen uitnodiging maken zonder iemand met git-toegang tot de projectenrepo, wat het hele zelfbedieningsprincipe van ZAD onderuithaalt. Ten tweede: `invites:` staat buiten het servicecontract, dus het krijgt niets van wat services wel krijgen (typed configmodel, drift-gelockt schemafragment, formuliergeneratie, detailpagina-blok, validatie op de savechokepoint). Ten derde: de sleutel is de enige toegangsdrempel en is zelfgekozen, dus in de praktijk raadbaar.

### 1.1 Drie bevindingen die je onderweg tegenkomt

**Dode vervaldatum-code.** `InviteManager.validate_invite` (`opi/manager/invite_manager.py:83` tot `121`) leest `invite.get("expires_at")`, parseert drie formaten en gooit `InviteExpiredError` (gedefinieerd op regel 31). `opi/api/invite_routes.py` heeft er vertaalde foutmeldingen voor (regel 263 en 281). `tests/test_invite_manager.py` heeft er zes tests voor (regels 25 tot 76). En `expires_at` staat niet in `$defs/invite`, dat `additionalProperties: false` heeft. Een invite met een vervaldatum laat dus het hele projectbestand door `validate_project_schema` vallen. Dit is precies de foutklasse die project dp-bn7 stil blokkeerde: code schrijft of leest een veld dat het schema verbiedt, de reprocess faalt, en niemand ziet het.

**Dezelfde fout in `settings`.** `get_invite_settings` (`project_file_handler.py:2947` tot `2969`) geeft defaults terug voor `allow_sso`, `allow_local`, `default_expiration_days` en `default_language`. Alleen `default_language` staat in `$defs/invites.settings`, dat ook `additionalProperties: false` heeft (regel 711 tot 717). De eerste drie kunnen dus nooit gezet worden; hun defaults zijn in de praktijk hardcoded gedrag.

**Sleutels zijn een globale naamruimte.** `_find_project_by_invite_key` (`opi/api/invite_routes.py:185` tot `225`) loopt alle projecten in de ProjectStore af en geeft de eerste treffer terug. Twee projecten met dezelfde sleutel betekent dat de tweede invite onbereikbaar is, zonder foutmelding, afhankelijk van de iteratievolgorde van de store. Uniekheid moet dus projectoverstijgend afgedwongen worden, niet per project.

---

## 2. Past het servicecontract hier eigenlijk op?

Eerlijk antwoord: voor het grootste deel wel, voor één deel niet, en op één punt moet je oppassen dat je niets forceert.

**Wat past.** `instructions/services.md` beschrijft een service als een configuration-as-code-eenheid die zijn eigen configvorm, formuliervelden, schemafragment en detailpagina-presentatie bezit. Dat is precies wat invites nodig heeft en vandaag mist. De `ConfigLayer.PROJECT`-laag (`opi/services/catalog/base.py:55`) is de juiste laag: uitnodigingen zijn projectbreed, niet per component en niet per deployment. `keycloak` en `sleep-mode` zijn de dichtstbijzijnde sjablonen, want die hebben allebei een project-level configsectie met een genest `Sequence`-veld.

**Wat niet past, en wat dus leeg blijft.** Een invite provisioneert niets. Er wordt geen resource aangemaakt bij het deployen, er is geen secret, er is geen manifest, en er is niets op te ruimen. De Keycloak-gebruiker ontstaat op het moment dat iemand de uitnodiging inwisselt, in de publieke route, niet in de provisioneringslus. Concreet blijven deze hooks op de default staan, en zet dat als commentaar in het package zodat de volgende lezer niet gaat zoeken:

| Hook | Blijft leeg omdat |
|---|---|
| `provision(ctx)` / `provision_order` | Er is geen deployment-resource. De gebruiker wordt bij inwisseling aangemaakt, buiten de deploycyclus. |
| `cleanup_manager_key` / `handle_service_removal(ctx)` | Bewust leeg. Een uitnodiging verwijderen mag de al aangemaakte realm-gebruikers niet raken: dat zijn legitieme gebruikers die niets met de uitnodiging te maken hebben zodra ze bestaan. |
| `manifest_secret_class` / `contribute_manifest_context` / `build_secret_files` | Geen envFrom-secret, geen sidecar, geen template-override. |
| `config_component_layout()` / `config_component_visualizers()` / `config_editables(COMPONENT)` | Geen componentlaag: een uitnodiging hoort bij het project, niet bij een container. |
| `config_approvals(layer)` | Geen goedkeuring nodig. De projectbeheerder mag zelf uitnodigen binnen zijn eigen realm. |

Dat is geen zwakte van het contract: `instructions/services.md` regel 43 zegt expliciet dat een gedragsloze service een paar regels is en dat defaults no-ops zijn. `attachments` op de projectlaag doet hetzelfde (`opi/services/catalog/attachments/__init__.py`): alleen `config_form_section`, verder niets.

**Waar je moet oppassen.** De inwisselstroom is een publiek HTTP-oppervlak met FastAPI, httpx en de Keycloak-connector. De catalogus moet importlicht blijven (`instructions/services.md` regel 302: "Keep the catalog import-light"). `sleep-mode` heeft dit al opgelost: `opi/services/catalog/sleep_mode/router.py` wordt niet door de registry geïmporteerd maar expliciet gebonden in `opi/server.py`. BESLIST (7.13): `opi/api/invite_routes.py` verhuist naar `opi/services/catalog/invite/router.py` volgens het sleep-mode-patroon, maar als aparte en laatste stap, zodat de diff puur verplaatsen is. Een verhuizing van 1034 regels tegelijk met een schemamigratie maakt de review onmogelijk.

**Wat je niet moet forceren.** Verzin geen provisioneringsstap "om de service compleet te maken", en gebruik `config_approvals` niet om een uitnodiging door een beheerder te laten goedkeuren tenzij de gebruiker daar expliciet om vraagt. Beide zijn speculatieve complexiteit.

---

## 3. Namen (vastgesteld)

Vastgesteld op 1 augustus. Niet meer ter discussie; wijk je af, koppel dat dan terug.

| Wat | Voorstel | Waarom, en het alternatief |
|---|---|---|
| `ServiceType`-waarde | `INVITE = "invite"` | Enkelvoud, consistent met de dertien andere services. Gevolg: de migratie is een hernoeming (`invites:` naar `services/invite/config`), geen pure verplaatsing. |
| Klassenaam | `InviteService` | Volgt `KeycloakService`, `SleepModeService`. |
| Packagemap | `opi/services/catalog/invite/` | Volgt de packageconventie sinds RC-5. |
| Configmodel | `InviteConfig`, met `InviteEntry` per item | Volgt `KeycloakConfig` + `KeycloakClientEntry`. |
| `config_section_id` | `"invite-config"` | Volgt `"keycloak-config"`, `"sleep-mode-config"`. |
| `modal_flow_id` | `"modal-edit-invite-config"` | Volgt `"modal-edit-sleep-mode-config"`. |
| Weergavenaam in de UI | `"Uitnodiging"` | Nederlands, zoals `"Slaapstand"` en `"Bijlagen"`. |
| Nieuwe optionsprovider | `InviteRealmRoleOptionsProvider` | Volgt `WakerComponentOptionsProvider`, die ook uit de omringende formulierdata leest. |
| Nieuwe validator | `InviteKeyValidator` | Volgt `AttachmentIdValidator` in vorm en foutmeldingstoon. |
| Nieuwe enforcer | `UniqueInviteKeyEnforcer` | Volgt `UniqueNamesEnforcer` / `UniqueDeploymentNameEnforcer`. |
| Schemaversie van de service | `config_schema_version = "1.0"`, fragment `invite.v1.0.json` | Verplicht formaat: `<servicenaam>.v<versie>.json` naast het package (`opi/services/config_schema.py:34`). |
| Projectschemaversie | `LATEST_SCHEMA_VERSION` van `2.5` naar `2.6` | `opi/services/schema_migration.py:18`. |

**Sleutelnamen: streepjes, en de lezers gaan via het model.** De configsleutels worden `realm-roles`, `restrict-domain`, `auth-methods`, `application-url`, `contact-email`, `success-title`, `success-button` en `default-language`, consistent met alle dertien andere services. Het model draagt de aliassen. De vijf invite-lezers (`invite_manager.py`, `invite_routes.py`, `project_file_handler.py` en de twee templates) gaan daarbij van rauwe `dict.get()` naar het model; dat is geen bijvangst maar het punt, want een service die zijn config bezit hoort ook de weg ernaartoe te bezitten. Omdat de servicenaam toch al verandert is de migratie sowieso een hernoeming, dus het argument "houd het een pure verplaatsing" is vervallen.

**Structuur: `settings` verdwijnt.** `settings` en `config` zijn hetzelfde begrip, dus twee niveaus met dezelfde betekenis. Het wordt `services/invite/config/default-language` naast `services/invite/config/active`. Komen er later projectbrede standaarden bij, dan staan die op datzelfde niveau.

---

## 4. Taken

> **Let op:** waar hieronder nog "voorstel" of "beslissing voor de gebruiker" staat, is die beslissing inmiddels genomen. Sectie 7 is het contract en wint bij tegenspraak; sectie 8 corrigeert feitelijke aannames in de tekst hieronder.


Elke taak heeft een verifieerbaar succescriterium. Taken die parallel kunnen staan gemarkeerd; de rest hangt van de vorige af.

### Taak 1: registreer de service (blokkeert alles behalve taak 10)

1. Voeg `INVITES = "invites"` toe aan `ServiceType` in `opi/services/services_enums.py`, in een eigen rubriek met commentaar, zoals `SLEEP_MODE` op regel 35.
2. Voeg een `ServiceDefinition` toe in `ServiceAdapter.SERVICE_DEFINITIONS` in `opi/services/services.py` (het blok begint rond regel 503 met `ServiceType.KEYCLOAK`). Velden: `name="Uitnodigingen"`, een beschrijving in het Nederlands, `icon` (kies uit de ROOS-iconenset, bijvoorbeeld `"envelop"` of `"gebruikers"`, controleer welke bestaan), `color`, `scope="component"` (dezelfde waarde als keycloak en attachments gebruiken; scope is hier niet betekenisvol maar het veld is verplicht), `variables=[]`, en cruciaal `requires=["services/keycloak"]`. Zet `hidden` NIET op True: dat is precies wat sleep-mode onzichtbaar maakte in de wizard.
3. Maak `opi/services/catalog/invite/__init__.py` met `class InviteService(Service)`, `service_type = ServiceType.INVITE`, en een module-docstring die uitlegt wat de service is en welke hooks bewust leeg blijven (neem de tabel uit sectie 2 over als proza).
4. Voeg één regel toe aan `SERVICES` in `opi/services/registry.py` (het dict begint op regel 40) plus de import bovenaan.

**Verify:** `uv run pytest tests/test_service_providers.py -q` slaagt. Deze test faalt als een `ServiceType` geen service heeft of als de definitie is afgedreven, dus groen betekent dat de registratie compleet is. Daarnaast: de service verschijnt op de `/services`-pagina en als kaart in de servicesstap van de aanmaakwizard.

### Taak 2: configmodel en schemafragment (na taak 1)

1. Maak `opi/services/catalog/invite/config_model.py` met twee Pydantic-modellen, naar het voorbeeld van `opi/services/catalog/keycloak/config_model.py`:
   - `I18nText` met `nl: str | None` en `en: str | None`, `extra="forbid"`. Dit vervangt `$defs/i18n-text`.
   - `InviteEntry` met `key: str` (verplicht), `roles: list[str]`, `realm_roles: list[str]`, `client_roles: dict[str, list[str]]`, `groups: list[str]`, `restrict_domain: str | None`, `auth_methods: list[str]`, `contact_email: str | None`, `application_url: str | None`, `message: I18nText | None`, `success_title: I18nText | None`, `success_button: I18nText | None`. Zet `extra="forbid"`: het huidige `$defs/invite` verbiedt extra's ook, en dat is de guardrail die we willen houden.
   - `InviteConfig` met `default_language: str = "nl"` en `active: list[InviteEntry]`, `extra="forbid"`.
2. Zet op de service `config_model = InviteConfig` en `config_schema_version = "1.0"`.
3. Implementeer `config_api_fields(layer)` dat `self.config_model_field_names()` teruggeeft voor `ConfigLayer.PROJECT` en `[]` voor de rest, zoals `SleepModeService.config_api_fields` (`opi/services/catalog/sleep_mode/__init__.py:40`).
4. Genereer het fragment: `uv run python -m opi.services.config_schema`. Dat schrijft `opi/services/catalog/invite/invite.v1.0.json`. Commit dat bestand.

**Verify:** `uv run pytest tests/test_service_config_schema.py -q` slaagt (de drift-lock tussen model en fragment). En: `InviteService().validate_config(<de invitesconfig van asses-k2n>)` valideert zonder fout. Schrijf dat als unittest met de echte vier configblokken als fixtures.

**Let op:** `roles` en `realm_roles` doen allebei hetzelfde (`assign_invite_permissions`, `opi/manager/invite_manager.py:270` tot `287`, voegt beide samen). Behoud allebei in het model zodat bestaande bestanden blijven valideren, maar bied in de UI alleen `realm_roles` aan en documenteer `roles` als verouderd in het docstring.

### Taak 3: lezers naar het servicepad, met terugvalpad (na taak 2)

Dit is de stap die de oude en de nieuwe locatie tegelijk laat werken tijdens de uitrol, precies zoals `get_domains_config` dat doet voor de verplaatsing van `domains:` naar publish-on-web (v2.4 naar v2.5).

1. Pas `ProjectFileHandler.extract_invites_config` aan (`opi/handlers/project_file_handler.py:2929`): lees eerst `services/invite/config` via `Project(project_data).get(...)` of `Project.service_config("invite")` (`opi/services/project.py:140`), en val terug op `project_data.get("invites", {})`. Alle zeven andere invitemethodes gaan door deze functie heen, dus dit is de enige plek die het weet. Controleer dat: `get_invite_settings`, `get_invite_by_key` en `get_all_active_invites` roepen alle drie `extract_invites_config` aan (regels 2961, 2982, 3001).
2. Als je in taak 3 het platslaan van `settings` doorvoert (zie sectie 3): laat `get_invite_settings` beide vormen lezen zolang het terugvalpad bestaat.
3. Verander niets aan `invite_manager.py` of `invite_routes.py`. Die praten alleen met `ProjectFileHandler`, dus die blijven werken.

**Verify:** een unittest die dezelfde inviteconfig één keer op de oude en één keer op de nieuwe plek zet en voor allebei dezelfde `get_invite_by_key`-uitkomst krijgt. En een handmatige rooktest in de sandbox: een project met de nieuwe locatie levert een werkende `/invite/<key>`-landingspagina op.

### Taak 4: het formulier, de editables en de visualizers (na taak 2, kan parallel met taak 5 en 6)

Dit is het grootste stuk werk en het deel dat vandaag volledig ontbreekt.

1. Maak `opi/services/catalog/invite/editables.py`, naar het model van `opi/services/catalog/keycloak/editables.py`. Gebruik `config_path(ConfigLayer.PROJECT, ServiceType.INVITE, "config", ...)` in plaats van hardcoded strings, zoals `opi/services/catalog/sleep_mode/editables.py:18` doet. Zet op elke editable `virtualize=("services", "_services-config")`: zonder die virtualisatie botst project-level serviceconfig met de serviceselectielijst in de wizardstate (`instructions/services.md` regel 141).
2. De velden, met per veld het voorstel voor widget, validator en converter:

| Yaml-pad (onder `services/invite/config`) | Widget | Validator/converter | Toelichting |
|---|---|---|---|
| `default_language` | select | `AllowedValuesValidator(["nl","en"])` | Nieuwe kleine optionsprovider of hergebruik van een bestaande ja/nee-achtige provider; zie taak 4.4. |
| `active` | sequence | `min_items=0`, `max_items` (voorstel 20) | De container; kinderen hieronder. |
| `active[*]/key` | text | `InviteKeyValidator` + `UniqueInviteKeyEnforcer` | Zie taak 7 voor de generatie-optie. |
| `active[*]/realm_roles` | sequence van selects | `RealmRoleValidator` op het item | Gevoed door `InviteRealmRoleOptionsProvider`, zie taak 6. |
| `active[*]/restrict_domain` | text | nieuwe `EmailDomainValidator` (voorstel) | Vandaag mag hier zowel `rijksoverheid.nl` als `@rijksoverheid.nl` staan; `validate_email_domain` (`invite_manager.py:143`) normaliseert zelf. Valideer op een geldige domeinvorm en normaliseer bij het opslaan naar de vorm zonder `@`. |
| `active[*]/contact_email` | text | `EmailValidator` (bestaat al, `validators.py:23`) | |
| `active[*]/application_url` | text | `UrlValidator` (bestaat al, `validators.py:228`) | |
| `active[*]/auth_methods` | checkbox_group of multi_select | `AllowedValuesValidator(["sso","local"])` | Leeg betekent: val terug op de projectinstellingen. Maak dat expliciet in de helptekst. |
| `active[*]/message/nl` en `/en` | textarea | geen | Zie taak 4.3 voor de i18n-vorm. |
| `active[*]/success_title/nl` en `/en` | text | geen | |
| `active[*]/success_button/nl` en `/en` | text | geen | |
| `active[*]/expires_at` | date | zie taak 9 | Alleen als de gebruiker kiest voor behoud van het veld. |

   `groups` en `client_roles` laat je bewust buiten de UI: geen van de vier live projecten gebruikt ze, ze blijven wel in het configmodel zodat handmatige YAML blijft valideren. Zeg dat in het configmodel-docstring, zoals `KeycloakClientEntry` het over "advanced pass-through" heeft.
3. **Het i18n-patroon.** Er is geen bestaande widget voor `{nl, en}`. Twee manieren, kies er één en leg de keuze vast:
   - **Voorstel:** twee losse editables per tekst, met de paden `active[*]/message/nl` en `active[*]/message/en`, gegroepeerd in een `Fieldset` met legenda "Welkomstbericht". Geneste paden binnen een sequence-item worden ondersteund; `opi/forms/editables/fields/deployments.py:153` gebruikt bijvoorbeeld `deployments[*]/components[*]/services/attachments/config[*]/reference`. Dit is de simpelste route en levert zes velden op (drie teksten maal twee talen).
   - Alternatief: één `Sequence` van `{lang, text}`-paren. Flexibeler, maar je krijgt er een validatieprobleem bij (dubbele talen) en de opslagvorm wijkt af van wat de vier bestaande projecten hebben. Niet doen zonder reden.
4. Maak `opi/services/catalog/invite/visualizers.py` met een `EditableVisualizer` per editable: label in het Nederlands, `help_text` waar het veld niet vanzelf spreekt, en de `children`-boom voor de sequences. Kopieer de opbouw van `opi/services/catalog/keycloak/visualizers.py:94` (`KEYCLOAK_ADDITIONAL_CLIENTS`), dat is exact het patroon voor "lijst van items met per item een naam en een geneste lijst".
5. Implementeer `config_editables(ConfigLayer.PROJECT)` en `config_form_section(ConfigLayer.PROJECT)` op de service. Cache de sectie in `self._config_section_cache` zoals keycloak en sleep-mode doen, want consumenten vergelijken sectie-identiteit. Zet `visible=self._config_selected` (de helper staat identiek in keycloak en sleep-mode) en `post_save_action="save_only"`: een uitnodiging wijzigen hoeft geen deploy te triggeren, want er verandert niets aan de manifests. Dat is een verschil met keycloak en sleep-mode, die `"process_project"` gebruiken; zet er commentaar bij zodat niemand het "corrigeert".
6. Layout: een `Fieldset` "Algemeen" met `default_language`, en een `Sequence(field_name=cp("active"))` in een `Fieldset` "Actieve uitnodigingen" met een beschrijving die uitlegt dat de link de enige toegangsdrempel is.
7. Bedraad de sectie op de drie plekken die `instructions/services.md` regel 144 tot 151 noemt, en vergeet er geen:
   - `opi/forms/visualizers/wizard_sections.py`: voeg `INVITE_CONFIG_SECTION = get_service(ServiceType.INVITE).config_form_section(ConfigLayer.PROJECT)` toe naast regel 296, en zet hem in `_CONFIG_SECTIONS_BY_ID` (regel 306). `SERVICE_CONFIG_SECTIONS` en `EDIT_SECTIONS` zijn daarvan afgeleid en hoeven niet apart bijgewerkt.
   - `opi/forms/visualizers/flows.py`: voeg de sectie toe aan `CREATE_FLOW` (regel 69), `EDIT_FLOW` (regel 93) en `MODAL_EDIT_SERVICES_FLOW` (regel 143), en maak een `MODAL_EDIT_INVITES_FLOW` naar het model van regel 190, plus een regel in `FLOW_REGISTRY` (regel 226). `SERVICE_CONFIG_MODAL_FLOWS` (regel 244) is afgeleid van `modal_flow_id` en gaat vanzelf.
   - Positie in `CREATE_FLOW`: er is geen afhankelijkheid van componenten, wel van de keycloakstap (de rollenselect leest daaruit). Zet de invitessectie dus NA `KEYCLOAK_CONFIG_SECTION`.

**Verify:** `uv run pytest tests/test_flow_registry_snapshot.py -q` faalt eerst (de flowsnapshot verandert), je actualiseert de snapshot bewust en de diff laat exact de vier nieuwe sectieplaatsingen zien. Daarna een user-based sandbox-E2E naar het model van `tests/e2e/test_sandbox_sleep_mode_ui.py` met `tests/e2e/helpers/service_config.py`: echte kliks, een invite aanmaken in de aanmaakwizard, hem wijzigen via de "Services beheren"-modal, en hem wijzigen via de eigen "Configureer"-knop op de detailpagina. Geen `page.evaluate`-omweg en geen directe modalfragment-URL's, want dan test je de bedrading niet.

### Taak 5: het blok op de detailpagina (na taak 2, parallel met 4)

Zonder dit ziet een projectbeheerder zijn eigen uitnodigingslinks nergens en moet hij ze uit YAML overtypen.

1. Implementeer `detail_page_sections(project_data, user_role)` op de service, naar het model van `KeycloakService.detail_page_sections` (`opi/services/catalog/keycloak/__init__.py:65`). Retourneer `[]` als er geen actieve invites zijn.
2. Maak `opi/services/catalog/invite/section-detail.html.j2` (de catalogusmap staat op het Jinja-zoekpad, zie `opi/core/templates.py`; adresseer het als `invite/section-detail.html.j2`). Toon per invite: de sleutel, de volledige link, de toegekende rollen, het contactadres, en een kopieerknop.
3. **De volledige link.** De hook krijgt geen `request` mee, en de publieke basis-URL komt in de inwisselroutes uit `request.url_for` (`opi/api/invite_routes.py:428`). Zet dus alleen de sleutel in `section.context` en bouw de absolute URL in het sjabloon met `url_for('invite_landing', key=...)`. Ga niet alsnog een basis-URL-instelling verzinnen.
4. **Rolafscherming.** BESLIST (7.9): alleen `admin` en `owner`, net als het keycloak-realmblok. De link is het geheim, dus wie hem ziet kan uitnodigen.
5. Let op de ROOS-valkuil: `<c-button>` hercodeert attribuutwaarden naar dubbele quotes, dus JSON of vierkante haken in `hx-vals`, `hx-headers` of `@click` breken. Gebruik voor de kopieerknop een enkelvoudige, enkel-gequote string of een globale JS-map, geen inline JSON. Raadpleeg `/Users/robbertuittenbroek/IdeaProjects/jinja-roos-components/ROOS_CLAUDE_REFERENCE.md` voordat je de knop schrijft.

**Verify:** een E2E of een rendertest die het blok op de detailpagina van een project met een invite laat zien, met een klikbare link die naar `/invite/<key>` verwijst, en die het blok NIET toont voor een gebruiker zonder de vereiste rol.

### Taak 6: de Keycloak-koppeling (na taak 4)

De service hoeft de rol niet te controleren; Keycloak bepaalt de toegang. Maar de service moet de rol wél kunnen dragen, en vandaag is dat vrije tekst.

1. **De afhankelijkheid declareren.** `requires=["services/keycloak"]` op de `ServiceDefinition` (taak 1). Dat pad-syntax-mechanisme doet drie dingen tegelijk volgens de docstring in `opi/services/services.py:177`: auto-selecteren, vergrendelen in de UI, en valideren bij het indienen. Keycloak zelf gebruikt hetzelfde met `requires=["services/publish-on-web"]` (regel 511). Bouw hier geen tweede mechanisme naast.
2. **De optionsprovider.** Maak `InviteRealmRoleOptionsProvider` in `opi/forms/visualizers/providers.py` en registreer hem in `PROVIDER_REGISTRY` (regel 934). Kopieer de vorm van `WakerComponentOptionsProvider` (regel 900 tot 917): die krijgt `yaml_data` in de constructor en leest daaruit de omringende formulierdata, zodat hij gevuld is in de bewerkstroom en leeg in de aanmaakwizard. De provider verzamelt twee bronnen en ontdubbelt:
   - `services/keycloak/config/realm-roles[*]/name`, het veld dat `KeycloakRealmRoleEntry` typeert (`opi/services/catalog/keycloak/config_model.py:96`, alias `realm-roles`).
   - `services/keycloak/config/restrict-access/realm-role` (`opi/services/catalog/keycloak/editables.py:48`, default `allowed-user`). Dit is belangrijk: alle vier de live projecten gebruiken `allowed-user`, en die rol staat NIET in `realm-roles`, hij komt uit de authorization wall. Een provider die alleen `realm-roles` leest, geeft in de praktijk een lege lijst.
3. **Keycloak niet geselecteerd.** Twee lagen, allebei nodig:
   - `requires` maakt het in de UI onmogelijk om invites te kiezen zonder keycloak.
   - Voor het geval het toch gebeurt (handmatige YAML, API): gebruik `FormSection.guard` en `guard_message` (`opi/forms/visualizers/sections.py:34` tot `42`) om de stap te blokkeren met de uitleg "kies eerst de Keycloak-service en stel minstens één realm-rol in", in plaats van een lege select te tonen.
4. **Een rol die later uit de Keycloak-config verdwijnt.** Dit is de scherpe rand en er zitten drie losse problemen in.
   - *Stille dataverliesrisico.* Als een select de huidige waarde niet in zijn opties heeft, valt hij bij de volgende opslag terug op de eerste optie of op leeg. Een invite die naar een verwijderde rol wees, wijst dan opeens naar een andere rol. Dit MOET je afvangen, ongeacht wat je verder kiest: de provider voegt de huidige opgeslagen waarde altijd toe als optie, gemarkeerd als "bestaat niet meer".
   - *Stille mislukking bij inwisseling.* `assign_invite_permissions` (`opi/manager/invite_manager.py:278` tot `287`) roept `assign_realm_roles_to_user` aan, zet ontbrekende rollen in `result["not_found"]`, voegt ze toe aan `errors`, en de inwisseling slaagt daarna gewoon. De uitgenodigde krijgt een account zonder rol, ziet een succespagina, en loopt vervolgens tegen de authorization wall aan zonder te weten waarom. BESLIST (7.11): een foutpagina, met het contactadres van de uitnodiging erop. Die pagina moet expliciet melden dat het account WEL is aangemaakt en dat opnieuw proberen niet werkt, want een tweede poging loopt stuk op `UserExistsError`. Gaat niet af bij een bewust rolloze uitnodiging (zie sectie 8).
   - *Voorkomen.* Optioneel: een enforcer die weigert de Keycloak-config op te slaan als er een rol verdwijnt waar een actieve invite nog naar verwijst. Voorstel is dit NIET te bouwen in de eerste ronde: het koppelt twee services aan elkaar via een enforcer en de waarschuwing plus de gemarkeerde optie dekken het praktische geval. Beslissing voor de gebruiker.

**Verify:** een unittest op de provider met drie fixtures: keycloak met `realm-roles`, keycloak met alleen `restrict-access.realm-role`, en geen keycloak. Plus een test die aantoont dat een opgeslagen rol die niet in de bronlijst voorkomt, toch als optie terugkomt en na een save-ronde onveranderd in het YAML staat.

### Taak 7: geheimhouding van de uitnodiging (na taak 4)

Vandaag is de sleutel zelfgekozen en raadbaar (`invulhulpen`). De link is de enige drempel, dus de link is het geheim. BESLIST (7.6): bouw alleen variant A. Variant B is bewust geparkeerd en variant C vervalt, want de vervaldatum gaat er in taak 9 juist uit. Vorm het sleutelveld wel zo dat B er later bij kan zonder een v2.0 van het fragment; als B komt, hoort het verbruik in OPI's eigen database en niet in het projectbestand (7.7).

**Variant A: gegenereerde willekeurige sleutel (aanbevolen, klein).**

De link wordt semi-geheim, zoals een niet-vermelde YouTube-video: wie hem heeft komt binnen, wie hem niet heeft raadt hem niet.

- Datamodel: ongewijzigd. `key` blijft één string. Alleen de manier waarop hij ontstaat verandert.
- Entropie: BESLIST (7.8) `secrets.token_urlsafe(16)`, dus 128 bits en 22 tekens. De link wordt geplakt, niet getypt. Dat weegt zwaarder nu er geen vervaldatum is: de link is permanent geldig.
- UI: een select "Sleutel" met "zelfgekozen" en "genereren". Bij "genereren" laat je het sleutelveld leeg en vul je het bij het opslaan.
- Waar genereren: `FormSection.post_merge` (`opi/forms/visualizers/sections.py:43`) krijgt `(project_data, wizard_data)` en mag `project_data` ter plekke muteren. Dat is de veilige plek: loop de `active`-lijst af en vul elke lege `key`. De `EditableGenerator`-route (`opi/forms/editables/editable.py:77`) is de andere kandidaat, maar generators worden op flowniveau geregistreerd (`generated_editables` op `CREATE_FLOW`, `flows.py:90`) en of dat binnen een `[*]`-sequence werkt is niet geverifieerd; controleer dat voordat je die route kiest.
- De zelfgekozen sleutel blijft dus gewoon bestaan, precies zoals gevraagd.

**Variant B: een vooraf gegenereerde set van X sleutels, eenmalig of beperkt bruikbaar.**

Dit is een wezenlijk andere feature dan A, en de kosten zitten niet in de UI maar in waar je de verbruikte sleutels bijhoudt.

- Datamodel in het projectbestand: `InviteEntry` krijgt bijvoorbeeld `codes: list[{code: str, max_uses: int, uses: int, used_at: str | None}]`, of een aparte lijst naast `active`.
- **Consequentie 1, de savechokepoint.** Vandaag LEEST de inwisselstroom alleen (`get_project_store().get_all()`, `invite_routes.py:202`). Verbruik bijhouden betekent schrijven, en de enige gevalideerde schrijfweg is `ProjectManager.save_and_commit_project` (`opi/manager/project_manager.py:1590`). Die valideert het schema, valideert de structurele integriteit, commit via de ProjectStore en pusht. Elke inwisseling wordt dus een git-commit op de projectenrepo.
- **Consequentie 2, git-churn.** Vijfentwintig uitgenodigde gebruikers zijn vijfentwintig commits, bovenop de commits van auto-tune en de wizard. De ProjectStore serialiseert en doet compare-and-swap, dus het is niet onveilig, maar het is wel ruis in een repo die ook de audittrail van projectwijzigingen is. Controleer bovendien of de git-monitor bij een wijziging van het projectbestand een reprocess van het project aanstoot: dan zou elke inwisseling een deploycyclus triggeren. Dat moet uit staan voor deze schrijfweg.
- **Consequentie 3, een ongeauthenticeerd verzoek dat een git-push veroorzaakt.** Dat is een schrijfversterking en dus een misbruikoppervlak. Er moet dan snelheidsbegrenzing op, en een afgebroken registratie mag geen code verbruiken (dus pas afboeken na een geslaagde gebruikersaanmaak, niet bij het openen van de landingspagina).
- **Consequentie 4, persoonsgegevens in git.** Wie `used_by: <e-mailadres>` in het projectbestand schrijft, zet dat permanent in de git-historie van de projectenrepo. Dat is onder de AVG niet meer te wissen. Sla dus geen e-mailadres op. Hooguit een teller en een tijdstip, of een gezouten hash. Dit is geen detail, benoem het expliciet in de feature-documentatie.
- **Aanbevolen alternatief voor de opslag.** Verbruik is runtime-toestand, geen declaratie, en hoort daarom niet in het declaratieve projectbestand. Twee betere plekken:
  - *OPI's eigen database.* OPI heeft SQLAlchemy en Alembic (`opi/migrations/`). Het projectbestand verklaart "deze uitnodiging heeft 25 eenmalige codes", de database houdt bij welke op zijn. Geen git-churn, geen persoonsgegevens in git, geen ongeauthenticeerde schrijfweg naar de projectenrepo.
  - *Keycloak als grootboek.* Na inwisseling bestaat de gebruiker in de realm; "verbruikt" is af te leiden uit een gebruikersattribuut met de code erin. Geen extra opslag, wel een extra Keycloak-bevraging per inwisseling.
- Eerlijke samenvatting: variant B in het projectbestand is bouwbaar maar sleept vier problemen mee die geen van alle bij variant A spelen. Als de gebruiker B wil, is het advies om de codes in het projectbestand te declareren en het verbruik in OPI's database bij te houden.

**Variant C: vervaldatum.** Zie taak 9. Een aflopende link is de goedkoopste beperking van een gedeelde link en de code bestaat al.

**Afweging in één zin per variant.** A kost een paar uur en haalt de link uit het raadbare; B kost een migratie, een grootboek en een nieuw misbruikoppervlak maar geeft echte eenmaligheid; C kost één veld en beperkt de schade van een gelekte link in de tijd.

**Verify voor A:** een test die een invite zonder sleutel opslaat en er een van 22 tekens uit krijgt die door `InviteKeyValidator` komt, plus een test die aantoont dat een zelfgekozen sleutel onveranderd blijft. Plus: twee projecten met dezelfde sleutel worden geweigerd door `UniqueInviteKeyEnforcer` met een melding die het andere project niet noemt.

### Taak 8: sleutelvalidatie en projectoverstijgende uniekheid (na taak 4)

1. `InviteKeyValidator` in `opi/forms/editables/validators.py`, naar het model van `AttachmentIdValidator` (regel 141). Voorstel: kleine letters, cijfers, streepjes en onderstrepingstekens, beginnend met een letter of cijfer, 3 tot 64 tekens. De sleutel wordt een URL-padsegment (`/invite/{key}`), dus geen spaties, geen slashes, geen procenttekens.
2. `UniqueInviteKeyEnforcer` in `opi/forms/editables/enforcers.py`. Dit MOET projectoverstijgend zijn, want `_find_project_by_invite_key` (`invite_routes.py:185`) geeft de eerste treffer in de hele ProjectStore terug. Lees de store, sla het eigen project over, en weiger bij een botsing.
3. Foutmelding: "Deze uitnodigingssleutel is al in gebruik." Noem het andere project NIET: dat lekt bestaan en eigenaarschap van sleutels van andere teams.
4. Koppel de fout aan het sleutelveld zelf, niet aan de sectie. De les uit de subdomeinwizard: een `FieldError` op een pad dat de gebruiker niet ziet, komt als een lege niet-vorderende stap aan. Log bovendien waarom een stap niet vordert.

**Verify:** een test met twee projecten in een stub-store die dezelfde sleutel proberen te zetten en een `FieldError` op het sleutelpad krijgen. En handmatig in de sandbox: de wizard toont de fout bij het veld en gaat niet stilzwijgend door.

### Taak 9: de expires_at-beslissing uitvoeren (na taak 2)

BESLIST (7.4): optie B, de code gaat eruit. Er komt dus geen vervaldatum. Optie A staat hieronder alleen nog als vastlegging van wat er is afgewogen.

**Optie A, het veld toevoegen (NIET GEKOZEN, hier alleen ter vastlegging).** Zet `expires_at: date | None = None` op `InviteEntry`, voeg een `date`-widget toe aan het formulier, en laat `validate_invite` staan. Kosten: één veld, één widget, één regel in het fragment. Baten: de bestaande vervalafhandeling met drie geparseerde formaten en de zes tests in `tests/test_invite_manager.py:25` tot `76` gaan van dode code naar werkende code, en een gedeelde link wordt in de tijd beperkt, wat variant C uit taak 7 is. Let op de bestaande semantiek: `expiry_date < today` (`invite_manager.py:118`) betekent dat de uitnodiging aan het EINDE van de opgegeven dag verloopt. Documenteer dat in de helptekst.

**Optie B, de code weghalen (GEKOZEN).** Verwijder `validate_invite`, `InviteExpiredError` (`invite_manager.py:31`), de aanroep in `get_valid_invite` (regel 202), de twee vertaalde foutmeldingen (`invite_routes.py:263` en `281`) en de zes tests. Kosten: je gooit werkende, getestte functionaliteit weg die de veiligste beperking van een gedeelde link is.

**In beide gevallen:** ruim de tweede variant van dezelfde fout op. `get_invite_settings` (`project_file_handler.py:2947`) geeft `allow_sso`, `allow_local` en `default_expiration_days` terug die geen van drieën door het schema toegelaten zijn. BESLIST (7.5): haal die drie eruit en houd alleen `default-language` over. Let op de correctie: `allow_sso` en `allow_local` zijn geen dode code maar onzetbare code, gebruikt als projectbrede terugval in `get_invite_auth_methods` (regel 3006). Die terugval wordt letterlijk "allebei toegestaan", wat vandaag feitelijk al gebeurt.

**Verify:** bij optie A: een test die een project met `expires_at` door `migrate_to_latest` haalt en daarna door `validate_project_schema`, en die slaagt. Bij optie B: `grep -rn "expires_at\|InviteExpired\|default_expiration_days" opi/` geeft nul treffers.

### Taak 10: migratie van de vier bestaande projecten (na taak 2 en 3)

1. Verhoog `LATEST_SCHEMA_VERSION` in `opi/services/schema_migration.py:18` van `2.5` naar `2.6`, en werk het commentaarblok op regel 20 tot 28 bij met een regel over deze verplaatsing (dat blok documenteert elke stap).
2. Schrijf `relocate_invites_to_service(project_data) -> bool` naar het model van `_migrate_v2_2_to_v2_3` (regel 673 tot 697), de verplaatsing van `config.keycloak` naar `services/keycloak/config/realms`. Die functie is het exacte precedent: hij gebruikt `Project(project_data).set("services/keycloak/config/realms", kc_list)` en vertrouwt erop dat `Project.set` de service-entry find-or-create't, en doet daarna `del config["keycloak"]`. Doe hetzelfde met `project_data["invites"]`.
3. Roep hem aan op twee plekken, en dit is het punt waar de bekende valkuil zit:
   - In `migrate_to_latest` als versie-gebonden stap: `if version < 2.6 and relocate_invites_to_service(project_data): migrated = True` (bij regel 107).
   - EN onvoorwaardelijk vanuit `_fixup_v2_data` (regel 421). De versie-gebonden stap slaat een bestand over dat al op `2.6` gestempeld is maar toch nog een top-level `invites:` heeft, bijvoorbeeld omdat een oude pod het tijdens een uitrol geschreven heeft. `migrate_to_latest` draait `_fixup_v2_data` altijd (regel 114, met het commentaar "Always run v2 fixups to clean up corruption from past bugs"): daar hoort de reparatie thuis. Dit is precies de schema-migration-catalog-gap.
4. Tweede valkuil, expliciet: migraties die alleen `deployments` en `components` aflopen missen top-level velden. `invites` IS top-level en `services` ook. Lees `project_data` direct; ga er niet van uit dat er al een `services`-lijst staat, en laat `Project.set` die zo nodig aanmaken.
5. Idempotent: zodra `invites` weg is, retourneert de functie `False`. Dubbel draaien mag niets doen en mag zeker geen tweede service-entry maken.
6. **Het projectschema.** Voorstel: laat `"invites"` voorlopig in de root-properties van `opi/schemas/project_v2.json` (regel 55) staan, samen met `$defs/invites`, `$defs/invite` en `$defs/i18n-text`. De root heeft `additionalProperties: false`, dus het meteen weghalen breekt elk bestand dat nog niet gemigreerd is, ook al is dat maar even. Dit is precies wat er met `domains` is gedaan: dat staat nog steeds op regel 57 terwijl het sinds v2.5 onder publish-on-web hoort. BESLIST (7.12): het opruimen gebeurt NIET in deze PR en ook niet "zodra alles gemigreerd is", want dat gebeurt aantoonbaar niet: 30 van de 47 productiebestanden dragen nog een vorm van voor v2.5. Markeer de defs als legacy met een `comment` en laat ze staan. Het opruimen hangt aan het per-versie valideren van het projectschema, dat als blokkerend punt in `TODO.md` staat.
7. **Verifieer op gemigreerde data, niet op het ruwe bestand.** De processtroom migreert in het geheugen VOORDAT hij valideert. Draai dus in de test eerst `migrate_to_latest()` en daarna `validate_project_schema()`. Een test die het ruwe bestand valideert, test het verkeerde ding; dat is de dp-bn7-les.

**Verify:** een test in `tests/test_schema_migration.py` die de vier echte projectbestanden inleest (of getrouwe fixtures ervan), `migrate_to_latest` draait, controleert dat `invites` weg is, dat `services/invite/config/active` de invite bevat, dat `validate_project_schema` slaagt, en dat een tweede `migrate_to_latest` `was_migrated=False` teruggeeft. Plus een test die een bestand met `schema-version: 2.6` én een top-level `invites:` erdoorheen haalt en aantoont dat de onvoorwaardelijke fixup het alsnog verplaatst.

### Taak 11: `instructions/services.md` uitbreiden met een sectie over editables (onafhankelijk, mag als eerste)

De gebruiker heeft hier expliciet om gevraagd. Formuleer het als werkbare richtlijn met verwijzingen naar bestaande voorbeelden, niet als losse zin.

**Let op, dit is de belangrijkste waarschuwing van deze taak.** `instructions/services.md` is op 28 juli door twee sessies tegelijk uitgebreid en dat geeft een conflict bij het samenvoegen naar main. Beide helften moeten erin, kiezen is fout. Op `uniform-declarative-platform-services` (commit `94d397e7`) staat de sectie "Forms and wizard screens": registratie levert nul UI op, wat `hidden=True` doet (de reden dat sleep-mode nergens in de wizard verscheen terwijl de service volledig werkte), een tabel per `ConfigLayer` met wat automatisch gaat en wat je met de hand bedraadt, en het verschil tussen `Editable` en visualizer. Op `branches-samenvoegen-naar-main` (`84571ed7` en verder) staat de `detail_page_sections`-hook, het hooksoverzicht, de alinea over de drie plekken waar een project-level configsectie bedraad moet worden, en de eis van een user-based sandbox-E2E. De merge-branch bevat het woord `hidden` nergens, dus juist de oorzaak van het sleep-mode-gat ontbreekt daar. Controle achteraf: `grep -c hidden instructions/services.md` op main mag niet nul zijn. Los dat conflict op VOORDAT je de nieuwe sectie schrijft, anders schrijf je in een halve versie.

Het bestand is in het Engels; schrijf de nieuwe sectie ook in het Engels, tenzij de gebruiker anders wil.

Plaats de sectie onder "Forms", als subsectie "Editables: validators, enforcers and closed sets". Inhoud:

1. **Elke editable declareert drie dingen.** Waar de waarde staat (`yaml_path`), welke waarden geldig zijn (`validator`), en hoe een ingediende string een opgeslagen waarde wordt (`converter`). Een veld zonder validator leunt op het JSON-schema, en dat vuurt pas bij het verwerken, niet bij het opslaan. Dat is precies hoe project dp-bn7 stil geblokkeerd raakte.
2. **Kies een select als de verzameling geldige waarden bekend en gesloten is** (cluster, template, probe-scheme, duur, rol), en vrije tekst als de waarde echt open is (een naam, een glob, een URL, een bericht). `opi/services/catalog/sleep_mode/editables.py` is het voorbeeld: acht van de negen velden zijn selects, alleen `match` is vrije tekst omdat het globpatronen voor nog niet bestaande deployments bevat.
3. **Elke select heeft een `OptionsProvider`** die geregistreerd is in `PROVIDER_REGISTRY` (`opi/forms/visualizers/providers.py:934`). Een provider die van omringende data afhangt, krijgt `yaml_data` in de constructor; `WakerComponentOptionsProvider` (regel 900) is het voorbeeld.
4. **Een select is geen validatie.** De browser stuurt wat hij wil, en de API- en YAML-paden lopen sowieso niet door het formulier. Zet achter een gesloten verzameling altijd óf `AllowedValuesValidator` (`validators.py:275`) óf een `Literal` in het configmodel, zoals `WakeMode` in `opi/services/catalog/sleep_mode/config_model.py:20`. Er moet ALTIJD gevalideerd worden of een waarde toegestaan is, ongeacht welke widget de gebruiker ziet.
5. **Laat een select nooit stil een waarde weggooien die hij niet kent.** Staat de opgeslagen waarde niet in de opties (een rol die uit de config verdween, een hernoemd component), voeg hem dan als gemarkeerde optie toe. Anders valt het veld bij de volgende opslag terug op de eerste optie en verandert de configuratie zonder dat iemand iets deed.
6. **Validator versus enforcer.** Een validator is per veld, synchroon, en geeft foutmeldingen terug (`EditableValidator`, `opi/forms/editables/editable.py:32`). Een enforcer is voor regels over velden heen of met I/O, is async, en gooit `ValueError`, `FieldError` of `FieldWarning` (`AsyncEditableEnforcer`, regel 50). Koppel een `FieldError` altijd aan een veld dat de gebruiker ook echt ziet; anders komt de fout aan als een stap die niet vordert zonder uitleg.
7. **Tabel met bestaande bouwstenen** zodat niemand een vijfde naamvalidator schrijft:

   | Nodig | Gebruik | Waar |
   |---|---|---|
   | Naam die een Kubernetes-resourcenaam wordt | `KubernetesNameValidator` | `validators.py:113` |
   | Componentnaam, inclusief uniekheid | `ComponentNameValidator` | `validators.py:84` |
   | Keycloak-rolnaam | `RealmRoleValidator` | `validators.py:200` |
   | URL | `UrlValidator` | `validators.py:228` |
   | E-mailadres | `EmailValidator` | `validators.py:23` |
   | Gesloten waardenverzameling | `AllowedValuesValidator` | `validators.py:275` |
   | Verplicht veld | `required=True` plus `RequiredValidator` | `validators.py:240` |
   | Uniekheid binnen een sequence | `UniqueNamesEnforcer` | `enforcers.py:60` |
   | Regel over meerdere velden met I/O | `DomainConfigEnforcer` als voorbeeld | `enforcers.py:192` |

8. **Vrijheid waar het moet.** Een select mag geen keurslijf worden waar de waarde echt open is. Een goede toets: kun je de volledige lijst geldige waarden op het moment van renderen kennen? Zo nee, dan is het vrije tekst met een validator, niet een select met een onvolledige lijst.
9. **Defaults.** `default=` op de editable is alleen wat het lege formulier toont; het configmodel is de guardrail. Houd ze gelijk en zaai geen defaults in een project dat de service niet gekozen heeft (de bestaande `{K}`-valkuil in de Traps-sectie).
10. **Optionele velden.** `remove_when_none=True` zodat een leeggemaakt veld de sleutel verwijdert in plaats van `null` te schrijven. `KEYCLOAK_RESTRICT_ACCESS_EDITABLE` (`opi/services/catalog/keycloak/editables.py:41`) is het voorbeeld.
11. **Afvinklijst voor een nieuw veld.** Klaar als het heeft: een `yaml_path` gebouwd met `config_path`, een veld in het configmodel, een validator of een gesloten select met `AllowedValues`, een visualizer met label en helptekst, en een regel in de layout van de sectie.

Werk daarnaast het openstaande punt uit `TODO.md` bij: `features/service-provider-registry.md` regel 25 beschrijft nog één module per service terwijl het sinds RC-5 een package is. Corrigeer die regel en verwijs door naar `instructions/services.md`.

**Verify:** `grep -c hidden instructions/services.md` is groter dan nul (beide merge-helften zitten erin), de nieuwe sectie staat onder "Forms", en elke verwijzing naar een bestand of symbool in de nieuwe tekst bestaat echt (loop ze na, een instructiedocument dat naar een verdwenen symbool wijst is erger dan geen document).

### Taak 12: feature-documentatie (na 4, 5, 6, 7, 9, 10)

Maak `features/invites.md` volgens de projectconventie: wat het is, hoe je het gebruikt, de configuratie, voorbeelden, afhankelijkheden. Neem er expliciet in op: dat de link het geheim is en wat dat betekent, de keuze rond de gegenereerde sleutel, de Keycloak-afhankelijkheid, en wat er gebeurt als een rol verdwijnt.

**Verify:** het bestand bestaat, gebruikt kebab-case in de bestandsnaam, en de YAML-voorbeelden erin komen door `validate_project_schema` heen.

### Taak 13 (optioneel, onafhankelijk, na 1): de router naar het package verhuizen

Verplaats `opi/api/invite_routes.py` naar `opi/services/catalog/invite/router.py` en bind hem expliciet in `opi/server.py`, precies zoals `opi/services/catalog/sleep_mode/router.py` gebonden wordt. De registry mag hem niet importeren, want dan trekt de catalogus FastAPI en httpx binnen. Doe dit als aparte PR NA de rest; een verhuizing van 1034 regels bovenop een schemamigratie maakt de review waardeloos.

**Verify:** `uv run pytest tests/e2e/ -m e2e -k invite` (of een handmatige rooktest op `/invite/<key>`) blijft groen, en `python -c "import opi.services.registry"` trekt geen FastAPI binnen.

---

### Taak 14: valideer het geheel tegen de reviewchecklist (als allerlaatste)

Loop de nieuwe service na tegen `instructions/service-review-checklist.md`, volledig, en neem de uitkomst op in de PR-beschrijving als een tabel met per sectie pass, fail of niet van toepassing.

Dit is geen formaliteit. De checklist bestaat uit de vallen die deze codebase in de praktijk heeft opgeleverd, en een nieuwe service loopt daar per definitie het meeste risico op. Let in het bijzonder op:

- sectie 0: stel via de registry vast wat de service declareert, niet via bestandsnamen;
- sectie 4: de editables zijn hier het grootste oppervlak, en de rolloze uitnodiging uit sectie 8 van dit plan moet een expliciete keuze zijn en geen lege waarde;
- sectie 7: elke regressietest moet je eerst hebben zien falen op de code zonder de fix;
- sectie 9: draai de audit over alle 47 productiebestanden, alleen lezend;
- sectie 10: elke toestandswijziging krijgt een logregel met wie, wat en voor welk project, en een idempotente no-op logt niets.

**Verify:** de tabel is volledig, elke fail is gerepareerd of staat als bevinding in de PR met een reden, en de volledige testsuite eindigt op nul failures en nul errors.

---

## 5. Volgorde en parallelliteit

```
Taak 11 (services.md)  ── onafhankelijk, mag als eerste, LOS committen
Taak 1 (registratie)
  └─ Taak 2 (configmodel + fragment)
       ├─ Taak 3 (lezers + terugvalpad)
       │    └─ Taak 10 (migratie)          <- na 2 EN 3
       ├─ Taak 4 (formulier + editables)
       │    ├─ Taak 6 (Keycloak-koppeling)
       │    ├─ Taak 7 (sleutelgeheimhouding)
       │    └─ Taak 8 (sleutelvalidatie + uniekheid)
       ├─ Taak 5 (detailpagina)            <- parallel met 4
       └─ Taak 9 (expires_at-beslissing)   <- parallel met 4
                                           Taak 12 (feature-doc) sluit af
                                           Taak 13 (routerverhuizing) apart, na alles
```

Taak 11 heeft nul code-afhankelijkheid en lost een merge-conflict op dat toch opgelost moet worden; doe hem eerst en commit hem apart, zodat hij niet in de review van de service verdwijnt. Taak 4, 5 en 9 kunnen door elkaar heen. Taak 10 moet samen met taak 3 landen: zonder het terugvalpad breekt de publieke inwisselroute op het moment dat het eerste bestand gemigreerd is en de nieuwe code nog niet overal draait.

## 6. Guardrails per ronde

Draai na elke taak die code raakt:

```bash
cd operations-manager/python
uv run pytest tests/test_service_providers.py tests/test_service_config_schema.py \
              tests/test_flow_registry_snapshot.py tests/test_schema_migration.py \
              tests/test_invite_manager.py tests/test_project_schema_validation.py -q
uv run ruff check . --fix && uv run ruff format . && uv run pyright
```

`tests/test_golden_manifests.py` hoort er niet bij: deze service draagt niets bij aan manifests, dus als die test verandert heb je per ongeluk iets aan de manifestlus geraakt en moet je terug.

Draai niet de hele testsuite; richt op de gewijzigde bestanden. In een verse worktree eerst `uv sync --all-groups`, anders faalt de pre-push-hook op ontbrekende testafhankelijkheden.

## 7. Genomen beslissingen (1 augustus 2026)

Alle veertien zijn beantwoord. Dit is het contract; wijk je af, koppel dat terug.

1. **Naam.** `ServiceType.INVITE = "invite"`, label `"Uitnodiging"`. Enkelvoud, consistent met de dertien andere services. Gevolg: de migratie is een hernoeming, geen pure verplaatsing.
2. **Sleutelnamen.** Streepjes, en de vijf lezers gaan van rauwe `dict.get()` naar het model. Zie sectie 3.
3. **Structuur.** `settings` verdwijnt; `default-language` staat naast `active` onder `config`.
4. **`expires_at` gaat eruit**, inclusief de vervaldatum-afhandeling en `InviteExpiredError` in `invite_manager`. Geen vervaldatum dus. Dat maakt de sleutel de enige bescherming, wat besluit 6 zwaarder maakt.
5. **`allow_sso`, `allow_local` en `default_expiration_days` gaan eruit** uit `get_invite_settings`. Let op: `allow_sso`/`allow_local` zijn geen dode code maar onzetbare code, gebruikt als projectbrede terugval in `get_invite_auth_methods`. Die terugval wordt letterlijk "allebei toegestaan", wat vandaag feitelijk al gebeurt. Per invite blijft `auth-methods` de enige weg om het te beperken.
6. **Alleen variant A: een genereerbare sleutel**, met de zelfgekozen sleutel als alternatief. Variant B (een set eenmalig bruikbare codes) is bewust geparkeerd. Vorm het sleutelveld zo dat B er later bij kan zonder een v2.0 van het servicefragment.
7. **Als B ooit komt: in OPI's eigen database**, niet in het projectbestand. Dat zou een ongeauthenticeerde inwisseling een schrijfweg naar git geven, met git-churn en persoonsgegevens in de historie als gevolg.
8. **22 tekens, ongeveer 128 bits.**
9. **Het detailpaginablok is voor `admin` en `owner`.** De link is het geheim, dus dit is een autorisatiekeuze en geen weergavekeuze. Volgt het Keycloak-realmblok (`catalog/keycloak/__init__.py:71`).
10. **Een rol die niet meer bestaat: markeren en waarschuwen, niet blokkeren.** Zie de correctie in sectie 8 over wélke rol dat is.
11. **Een inwisseling waarbij een genoemde rol niet toegekend kon worden toont een foutpagina.** Die pagina moet expliciet melden dat het account wél is aangemaakt en dat opnieuw proberen niet werkt, want een tweede poging loopt stuk op `UserExistsError`. Geldt niet bij een bewust rolloze uitnodiging, zie sectie 8.
12. **De oude defs in `project_v2.json` blijven staan**, als legacy gemarkeerd met een `comment`, precies zoals `domains`. Ze weghalen kan pas als het projectschema per versie gevalideerd wordt; dat staat als blokkerend punt in `TODO.md`. Gemeten: 30 van de 47 productiebestanden dragen nog een vorm van vóór v2.5, dus wachten tot alles zichzelf herschreven heeft werkt niet.
13. **De routerverhuizing gebeurt wel, maar apart en als laatste**, zodat de diff puur verplaatsen is en makkelijk terug te draaien als er iets misgaat in de publieke inwisselstroom.
14. **De nieuwe sectie in `instructions/services.md` is Engels**, consistent met de rest van dat bestand. Let op de merge-conflictwaarschuwing in taak 11: dat bestand is op 28 juli door twee sessies uitgebreid en beide helften moeten erin.

---

## 8. Correcties op dit plan, gevonden tijdens het besluitvormen

Deze weerleggen aannames die eerder in dit document staan. Waar ze botsen, wint deze sectie.

**De rollen komen niet uit `realm-roles`.** Gemeten over alle 47 productiebestanden: `realm-roles` in de Keycloak-config is in **geen enkel project** gedefinieerd. De rol die invites toekennen komt uit `restrict-access/realm-role`, gezet in 9 projecten, altijd op `allowed-user`. De optionsprovider moet dus dáár primair uit lezen. Een enforcer op `realm-roles` zou nooit afgaan.

**Een uitnodiging zonder rol is een eerstelijns keuze, geen weglating.** Dat wordt vandaag gebruikt om mensen alleen een lokaal account te laten aanmaken, zonder verdere rechten. `assign_invite_permissions` slaat een lege rollenlijst gewoon over (`invite_manager.py:278`, `if all_realm_roles:`). Het rolveld is dus optioneel en de select krijgt een expliciete optie "geen rol toekennen" met uitleg erbij. De waarschuwing uit besluit 10 en de foutpagina uit besluit 11 mogen hier niet afgaan.

**`roles` en `realm_roles` zijn twee velden die hetzelfde doen** en worden samengevoegd op `invite_manager.py:276`. Het model brengt dat terug tot één veld; `roles` wordt een alias of verdwijnt in de migratie.

**Invite-sleutels zijn een globale naamruimte.** `_find_project_by_invite_key` neemt de eerste treffer over álle projecten, dus uniekheid moet projectoverstijgend afgedwongen worden en niet per project. Dat is ook een argument vóór de gegenereerde sleutel uit besluit 6: die maakt botsing én raadbaarheid tegelijk onwaarschijnlijk.

**De service-opzet waar dit plan op bouwt bestaat inmiddels.** Sinds 1 augustus hebben dertien van de vijftien services een configmodel en een drift-gelockt schemafragment, valideert `validate_service_configs` alle vier de configlagen, en bestaat `Service.config_model_for(layer)` voor een service die per laag een ander model draagt. Bouw hierop en niet op de oude beschrijving.
