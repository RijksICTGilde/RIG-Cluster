# Invoer eerst schoon, dan getoetst

## Waarom

Tim kon iemand niet aan zijn project toevoegen omdat er een spatie voor het e-mailadres stond. Dat is geen losse bug, het is hoe de editables vandaag werken: de validator krijgt de ruwe invoer te zien, en niets haalt eerst de randen eraf. `EmailValidator` toetst `^[^@\s]+@[^@\s]+\.[^@\s]+$`, dus `" jan@voorbeeld.nl"` krijgt "Geen geldig e-mailadres" terwijl het adres klopt.

Hetzelfde patroon zit op meer plekken:

- `COMPONENT_IMAGE` heeft een `ContainerImageConverter` die strip en lowercase doet, maar `ContainerImageValidator` draait ervoor en weigert de spatie al. De converter komt nooit aan de beurt.
- `DOMAIN_CUSTOM_BASE_DOMAIN`: `CustomDomainValidator` strip en lowercase alleen voor zijn eigen toets. `_resolve_deferrals` kopieert daarna de ruwe waarde naar `base-domain`. `" Voorbeeld.nl "` gaat dus door en wordt zo opgeslagen.
- De processor toetst required met `not value`, dus `"   "` telt als ingevuld. De API toetst met `RequiredValidator`, die eerst strip. Dezelfde editable geeft dus via formulier en API een ander antwoord.
- De API valideert een kopie (`model_dump()`) en schrijft daarna het eigen Pydantic-model weg. Converters draaien op de API nooit: geen enkele aanroeper geeft `convert=True` mee aan `validate_api_payload`.

In productie staat het resultaat al: `display-name` en `description` van `dd-mco` en de registry-naam `" GitHub MinBZK"` in `wgeo-nuo` hebben spaties aan de randen (meting hieronder).

Bij de inventaris kwamen ook twee injectieplekken boven, dezelfde klasse als de authwall-YAML-injectie die met `| tojson` gerepareerd is (`tests/test_authwall_banner_yaml_injection.py`). Die staan in taak 7.

## Waar dit plan tegen gemeten is

`forgejo/main` op `9968f4716` (14-10-2026). Bestandsnamen en symbolen zijn leidend, regelnummers zijn indicatief. `validators.py` en `converters.py` verschoven daar recent met de `LogoutPath*`-klassen.

### Productiedata

Gemeten op 14-10-2026 over de 67 projectbestanden in de lokale checkout van de projects-repo (stand 09-10-2026), 11.756 stringwaarden. Alleen-lezend.

| bevinding | aantal | paden | gevolg voor het ontwerp |
|---|---|---|---|
| spaties aan de randen, niet-geheim | 5 | `display-name`, `description`, `services[*]/config/registries[*]/display-name` | precies de foutklasse; trim lost het op bij de volgende save |
| spaties aan de randen, versleuteld | 17 | `registries[*]/password`, `client-secret`, `user-env-vars`, `remote-sources/*` | **het SECRET-profiel mag niet trimmen** |
| meerregelig en langer dan 1000 tekens | 3 | `components[*]/command[*]` en `deployments[*]/components[*]/command[*]` (vlam-wt8, heredoc-scripts) | **het commandoveld is MULTILINE**, niet LINE |
| langer dan 1000 tekens | 1 | `disabled-reason` | machinewaarde, de store-backstop toetst geen lengte |
| NUL, bidi, zero-width, NBSP, control-tekens, niet-NFC | 0 | | de backstop in taak 6 weigert niets dat er nu staat |

### Wat er al is

- `opi/forms/editables/pipeline.py`: `validate_field`, `validate_fields`, `convert_fields`, `enforce_rules`. Alleen de API gebruikt dit.
- `opi/api/validation.py`: profielen die API-velden op de *gedeelde* editables zetten, plus `validate_api_payload`. De richting klopt: de API toetst met dezelfde editables als het formulier. Dit plan maakt dat ook voor sanitizen waar.
- `EditableFormProcessor` in `opi/forms/editables/processor.py`: elke bladwaarde loopt `_read_submitted` → `_effective_value` → `_validate_field` → `_write_field`, op vier plekken: top-level, `_process_group_json`, `_process_sequence_json` en `_process_nested_sequence_json`. Dit is de choke point voor wizard, detail-edit en het admin-gebruikersformulier.
- `GitProjectStore._validate` in `opi/services/project_store.py`: elke schrijfweg naar het projectbestand komt erlangs. Het valideert alleen en verandert niets.
- Strippen gebeurt nu verspreid in converters (`IntegerConverter`, `KeyValueConverter`, `ContainerImageConverter`, `CommandLineConverter` en meer) en ad hoc in routers (`router_user_admin.py`, `invite_routes.py`, `project_utils.py`). Een generieke stap bestaat niet. `EmptyToNoneConverter` strip niet.
- Herbruikbaar: `normalize_base_domain` (`opi/utils/naming.py`), `RealmRoleValidator`, `EnvNameValidator`, `ContainerImageValidator`/`ContainerImageConverter`.

## Uitgangspunten

Uit het onderzoek naar Django, Symfony, Laravel, Rails, Pydantic, WTForms, Zod en OWASP. Bronnen staan onderaan.

1. **Server-side frameworks trimmen standaard en zonderen geheimen uit.** Django `CharField(strip=True)` met `strip=False` op wachtwoorden, Symfony `trim: true` met `PasswordType` op `false`, Laravel `TrimStrings` met een `$except` voor `password`. Schema-bibliotheken (Pydantic, Zod) doen standaard niets. Wij volgen de frameworks: aan, tenzij het veld anders zegt.
2. **Volgorde: canonicaliseren, sanitizen, valideren, opslaan, encoderen per sink.** Een sanitizer die na de validator draait verandert een goedgekeurde waarde (CWE-180). Dat is precies onze imagefout.
3. **Een sanitizer mag alleen dingen doen die geen betekenis veranderen.** Trim, NFC, `\r\n` naar `\n`, onzichtbare tekens aan de randen. Gevaarlijke tekens (NUL, bidi-overrides, een newline in een eenregelig veld) haal je niet stil weg maar **weiger** je met een melding. Stil wegfilteren verbergt een aanval en verrast de gebruiker.
4. **Input-sanitizing is geen XSS- of injectieverdediging.** Die zit aan de sink: Jinja-autoescape voor HTML (staat aan in `templates_lotc`), `| tojson` voor YAML-manifests, argv-lijsten voor subprocessen, `psycopg.sql` voor SQL. Een sanitizer die `<>'"` of YAML-tekens weghaalt is een anti-patroon: het beschadigt geldige waarden en beschermt niets. Dit plan doet dat dus niet.
5. **Python's `str.strip()` pakt NBSP (U+00A0) wel, en zero-width space (U+200B), BOM (U+FEFF) en word joiner (U+2060) niet.** Die laatste drie komen mee als je plakt uit Teams, Word of Confluence. Ze moeten er expliciet af.
6. **Lengte begrenzen vóór de regex.** Dat beschermt tegen ReDoS in validators die we niet allemaal nagelopen hebben.

## Ontwerp

### Een invoerprofiel per editable

Nieuw bestand `opi/forms/editables/sanitizers.py`. Een profiel is een geordende lijst sanitizers en een geordende lijst guards, die sequentieel worden doorgelopen. Geen grote functie die alles doet.

```python
class EditableSanitizer(Protocol):
    def sanitize(self, value: str) -> str: ...

@dataclass(frozen=True)
class InputProfile:
    sanitizers: tuple[EditableSanitizer, ...]
    guards: tuple[EditableValidator, ...]

LINE = InputProfile(
    sanitizers=(UnicodeNfc(), NormalizeLineEndings(), StripInvisibleEdges(), StripWhitespace()),
    guards=(MaxLength(2048), RejectControlChars(allow_newline=False), RejectBidiControls(), RejectInvisibleChars()),
)
MULTILINE = InputProfile(
    sanitizers=(UnicodeNfc(), NormalizeLineEndings(), StripInvisibleEdges(), StripWhitespace()),
    guards=(MaxLength(65536), RejectControlChars(allow_newline=True), RejectBidiControls()),
)
SECRET = InputProfile(sanitizers=(), guards=(MaxLength(65536), RejectNul()))
```

Op `Editable` komt één veld: `input_profile: InputProfile = LINE`. Uitzetten gaat door expliciet een ander profiel te kiezen. Een veld dat iets bijzonders nodig heeft kan een eigen `InputProfile(...)` meegeven, maar dat is geen reden om er nu al een vierde voorgedefinieerde te maken.

Waarom op `Editable` en niet op de visualizer: het profiel is een eigenschap van de data, net als `summarizer` (zie de docstring daar), en de API heeft geen visualizer. Een API-veld zonder UI krijgt zo dezelfde behandeling als een formulierveld.

Het profiel werkt alleen op `str`. Lijsten en dicts gaan ongemoeid door. Sequence-kinderen zijn bladwaarden en worden elk apart behandeld. `KEY_VALUE` houdt zijn eigen `KeyValueConverter`.

Volgorde per veld:

```
ruwe waarde → profile.sanitizers → profile.guards → required → validator → converter.write → opslaan
```

Wat bewust **niet** in het standaardprofiel zit:

- **Leeg naar `None`** (Laravel `ConvertEmptyStringsToNull`). `_write_field` slaat `None` over (`if value is None: return`), dus een veld leegmaken zou niet meer werken. Een getrimde `""` blijft `""`, en `remove_when_none` werkt zoals nu.
- **NFKC en lowercase.** Die veranderen betekenis. Ze horen per veld in een converter, zoals `ContainerImageConverter` al doet.
- **Een confusables-check (UTS #39).** Identifiers zijn al ASCII via hun validators. Vrije Unicode-namen die uniek moeten zijn hebben we niet.

### Een functie die schoonmaakt en toetst

In `pipeline.py` komt de functie die zowel formulier als API gebruikt:

```python
def prepare_field(editable: Editable, value: Any, context: dict[str, Any] | None = None) -> tuple[Any, list[str]]:
    """De schone waarde en de fouten erop. Wie opslaat, slaat de schone waarde op."""
```

`validate_field` wordt daar een dunne wrapper omheen. `EditableFormProcessor._validate_field` gaat er ook doorheen, zodat er één required-regel overblijft in plaats van twee. Let op een verschil: de processor weigert `False` op een verplichte checkbox (`not value`) en `RequiredValidator` laat `False` door. De verplichte bevestigingsvinkjes in `publish_on_web/editables.py` leunen op het processorgedrag. Dat gedrag moet blijven, dus de gedeelde regel behandelt `False` als ontbrekend. Controleer daarbij of een API-profiel een verplichte bool heeft die nu onbedoeld gaat weigeren.

## De taken

### 1. Het profiel en de sanitizers

`opi/forms/editables/sanitizers.py` met de protocol, `InputProfile`, de sanitizers en guards uit het ontwerp, en `LINE`, `MULTILINE`, `SECRET`. Op `Editable` het veld `input_profile: InputProfile = LINE` met een docstring in de stijl van `summarizer`.

De guards geven Nederlandse meldingen die de gebruiker verder helpen en het teken bij naam noemen. Bijvoorbeeld: "Dit veld bevat een onzichtbaar teken (U+202E) dat hier niet hoort. Typ de waarde opnieuw in plaats van hem te plakken." Een newline in een LINE-veld: "Dit veld is één regel; er staat een regelovergang in."

Bidi-controls zijn U+202A t/m U+202E en U+2066 t/m U+2069. U+200E/U+200F (LRM/RLM) en U+200C/U+200D (ZWNJ/ZWJ) blijven toegestaan, want die zitten legitiem in Arabische tekst en emoji. `RejectInvisibleChars` weigert U+200B, U+FEFF en U+2060 midden in een waarde. Aan de randen haalt `StripInvisibleEdges` ze al weg.

→ verify: unit-tests per sanitizer en guard in `tests/forms/test_sanitizers.py`. Daarin minstens: NBSP aan de rand weg, U+200B aan de rand weg en midden in geweigerd, U+202E geweigerd met de codepoint in de melding, `\r\n` wordt `\n`, NFC-samenvoeging van `e` + U+0301, SECRET laat `"  abc\n"` byte-exact staan, MaxLength weigert vóór de rest draait.

### 2. Eén weg door de pipeline

`prepare_field` in `pipeline.py`. `validate_field` en `EditableFormProcessor._validate_field` gaan erdoor. In de processor komt op alle vier de leesplekken de sanitize-stap direct na `_read_submitted` en vóór `_effective_value`. `_write_field` schrijft daarna de schone waarde. Daarmee wordt ook de ruwe deep-merge overschreven die `_process_sequence_json` eerst doet.

De gedeelde required-regel: `None`, een lege string na sanitize, een lege lijst en `False` zijn ontbrekend.

`DOMAIN_CUSTOM_BASE_DOMAIN` krijgt een converter die `normalize_base_domain` aanroept, zodat `_resolve_deferrals` een genormaliseerde waarde kopieert. Schrijf geen nieuwe normalisatie.

→ verify:

- Een processortest die `" jan@voorbeeld.nl "` als teamlid indient en `jan@voorbeeld.nl` terugleest. Dat is Tim's geval, en de test is rood op `9968f4716`.
- `" nginx:1.27 "` als component-image wordt geaccepteerd en als `nginx:1.27` opgeslagen.
- `"   "` op een verplicht veld geeft "Dit veld is verplicht", zowel via `process_json_submission` als via `validate_field`.
- `" Voorbeeld.nl "` als eigen domein landt als `voorbeeld.nl` in `base-domain`.
- De bestaande suites `tests/forms/` en `tests/test_detail_edit.py` blijven groen.

### 3. De uitzonderingen expliciet maken, met een guard die ze bewaakt

Profielen zetten:

- SECRET op elke editable met een `PASSWORD`-visualizer of een AGE-converter (`AGEEncryptConverter`, `ProjectAgeSecretConverter` en verwanten). Eerste bekende: `REGISTRY_PASSWORD` in `image_registries/editables.py`.
- MULTILINE op elke `TEXTAREA` en op `COMPONENT_COMMAND`. Dat laatste volgt uit de meting: vlam-wt8 heeft meerregelige commando's, en LINE zou het bewerken van dat component breken.

Een test in de stijl van `tests/test_schrijvers_inventaris.py` die alle visualizers verzamelt, inclusief die van `opi/services/catalog/**`. Hij faalt als:

- een `PASSWORD`-widget of AGE-converter niet SECRET heeft;
- een `TEXTAREA` LINE heeft;
- een editable SECRET heeft zonder een van die twee kenmerken. Die laatste vangt het uitzetten "omdat het makkelijk was"; kan het echt niet anders, dan staat het veld met een reden op een allowlist in de test.

→ verify: de test is groen, en rood te krijgen door het profiel van `REGISTRY_PASSWORD` of van één TEXTAREA weg te halen. Beschrijf die rood-check in de PR.

### 4. De API slaat op wat hij gevalideerd heeft

`validate_api_payload` geeft altijd de schoongemaakte payload terug: sanitize uit het profiel, en de converter als `convert=True`. Elke aanroeper gebruikt die terugwaarde om zijn model opnieuw op te bouwen, bijvoorbeeld:

```python
component_data = ComponentCreate.model_validate(
    await validate_api_payload(component_data.model_dump(), ADD_COMPONENT_VALIDATORS)
)
```

Het gaat om de aanroepen in `opi/api/router.py` (v1, zeven stuks) en `opi/api/v2/router.py` (v2, zeven stuks, waaronder de service-actions bij `action.editables_for(verb)`). Let bij de service-actions op `values.update(uploads)` erna: de uploads blijven buiten het profiel.

Een AST-test die faalt als een `validate_api_payload(...)`-aanroep als losse expressie staat, dus als het resultaat weggegooid wordt. Gebruik AST en geen grep, om dezelfde reden als in `test_schrijvers_inventaris.py`.

→ verify: een API-test die `{"image": " nginx:1.27 "}` via v2 component-add stuurt en `nginx:1.27` in het projectbestand terugleest; de AST-test is rood met één teruggezette aanroep.

### 5. API-velden die nu buiten de editables om gaan

Op verzoek, en vanuit hetzelfde principe: een veld dat via de API binnenkomt hoort door een editable te lopen, ook als het niet in de UI staat.

- **API v1 project create** (`api/router.py`, `user_email[]` en `contact_email`). `project_utils.py` strip de e-mails maar valideert ze niet. `contact_email` wordt helemaal niet getoetst. Een profiel in `api/validation.py`: `user_email` op de bestaande `USER_EMAIL_EDITABLE` uit `fields/team.py`. Voor `contact_email` een editable op `config/contact-email` met `EmailValidator`.
- **API v2 project create** (`api/v2/router.py`): `display_name` en `description` op de bestaande `DISPLAY_NAME`- en `DESCRIPTION`-editables uit `fields/identity.py`.
- **Invite-registratie** (`api/invite_routes.py`): e-mail, voornaam en achternaam hebben een eigen regex en een eigen strip. Zet ze op editables met de gedeelde `EmailValidator`. Is de eigen regex strenger om een reden die in Keycloak ligt, schrijf dat dan in de PR en houd de reden vast in de validator, niet in de route.
- **Attachments `validate-id`** (`web/router_wizard_attachments.py`) strip niet, terwijl de upload dat wel doet. Na taak 2 gebruiken beide `prepare_field` en vervalt de handmatige strip bij de upload.
- **Admin-gebruikersformulier** (`web/router_user_admin.py`): de `.strip()`-aanroepen na de processor zijn na taak 2 dubbel. Haal ze weg als de waarde uit de resultaat van de processor komt; laat ze staan als ze iets anders lezen, en zeg dat in de PR.

**E-mail normaliseren (besloten 14-10-2026):** een `EmailConverter` die het hele adres lowercase opslaat, op `USER_EMAIL`, het admin-e-mailveld en `INVITE_CONTACT_EMAIL`. Formeel is de local-part hoofdlettergevoelig. Maar `project_authorization.py`, `user_service.add_allowed_emails` en de db-console vergelijken al lowercase, dus in de praktijk is het dat al. Opslaan zoals het vergeleken wordt haalt een verschil weg.

→ verify: een test per route met spaties en hoofdletters in de invoer die het opgeslagen resultaat controleert; `contact_email` met `"a@b.nl\nkind: Secret"` wordt geweigerd met 422.

### 6. Velden zonder validator, en de backstop in de store

Gaten uit de inventaris. Gebruik steeds wat er al is:

| editable | bestand | wat erbij komt |
|---|---|---|
| `KEYCLOAK_ROLE_NAME` (`realm-roles[*]/name`) | `keycloak/editables.py` | `RealmRoleValidator`, die de invite-rollen al gebruiken |
| `KEYCLOAK_CLIENT_NAME` (`additional-clients[*]/name`) | `keycloak/editables.py` | een validator op het client-id. Kijk eerst of `KubernetesNameValidator` of `SlugValidator` past met wat Keycloak accepteert en wat in productie staat |
| `DEPLOYMENT_COMP_IMAGE` | `fields/deployments.py` | `ContainerImageValidator` en `ContainerImageConverter`, net als `COMPONENT_IMAGE` |
| `DEPLOYMENT_COMP_ATTACHMENT_USE_ENV_NAME` | `fields/deployments.py` | `EnvNameValidator`; de service-kopie in `attachments/editables.py` heeft hem al. Hergebruik die editable als dat kan, in plaats van een tweede |

Toets elke nieuwe validator eerst tegen de waarden in de projects-repo, zodat hij geen bestaand project onbewerkbaar maakt.

**Backstop in `GitProjectStore._validate`:** weiger in elke stringwaarde van het projectbestand NUL, de overige Cc-tekens behalve `\t` en `\n`, en de bidi-controls. Geen trim, geen lengte, niets dat iets verandert. Het vangt de schrijfwegen die geen editable kennen: approvals (ruwe JSON-body), service-config PUT/PATCH en programmatisch schrijven. Volgens de meting staat geen van deze tekens nu in productie.

→ verify: unit-test op de backstop met een NUL en een U+202E in een geneste waarde; de bestaande store-tests blijven groen.

### 7. De twee injectieplekken aan de sink

Gevonden bij de inventaris en in de code nagelopen. Dit is outputencoding, los van alles hierboven, en het is de eigenlijke verdediging.

- `manifests/issuer-letsencrypt.yaml.jinja`: `email: {{ contact_email }}` staat kaal. Via API v1 komt `contact_email` ongetoetst in `config/contact-email` (zie taak 5). Een waarde met een newline injecteert dan sleutels of een tweede document in het Issuer-manifest. Wordt `| tojson`, en `issuer_name` erbij.
- `manifests/db-console-emails-configmap.yaml.jinja`: `{{ email }}` staat kaal in een block scalar. Teamleden-e-mails komen via API v1 alleen gestript binnen. Render `emails.txt` als één `tojson`-string van de samengevoegde regels.
- `opi/connectors/kubectl.py` `_run_kubectl_command`: met `stdin_input` draait het `create_subprocess_shell` met een `<<'EOF'`-heredoc, en argumenten worden alleen gequote als er een spatie in staat. Vandaag niet uitbuitbaar gevonden, want de manifests die erdoor gaan zetten gebruikerswaarden met `tojson`. Maar het is een shell waar geen shell nodig is. Vervang door `create_subprocess_exec` met `stdin=PIPE` en `communicate(input=...)`. De zestien aanroepers veranderen niet.

→ verify: een injectietest in de vorm van `test_authwall_banner_yaml_injection.py` voor beide templates, rood op `9968f4716` en groen erna; een test die `_run_kubectl_command` met `stdin_input` aanroept en controleert dat er geen shell gestart wordt (patch `create_subprocess_shell` zodat hij faalt als hij aangeroepen wordt).

### 8. Documentatie

`features/invoerprofielen.md`: wat een profiel is, de volgorde, welke profielen er zijn en wanneer je welke kiest, wat er bewust niet in zit (leeg naar `None`, NFKC, tekens weghalen tegen XSS), en de bronnen hieronder. In `instructions/services.md`, bij het toevoegen van een veld, één alinea: welk profiel je kiest, en dat de guard uit taak 3 je anders tegenhoudt. `features/editables-framework.md` noemt `input_profile` bij de dataclass en in de volgorde van de pipeline.

→ verify: de docs noemen alleen symbolen die bestaan (`git grep` op elk genoemd symbool).

## Volgorde

1 → 2 → 3 is de kern en hoort samen te gaan: zonder 3 krijgt een geheim het LINE-profiel en wordt het getrimd. 4 en 5 bouwen op 2. 6 en 7 staan los en kunnen eerder. 8 als laatste.

Gate vóór de merge: draai de guards en de backstop uit taak 1 en 6 in dry-run over alle projectbestanden in de projects-repo (`~/IdeaProjects/rig-cluster-test-git-repositories/rig-cluster-projects-github/projects`, eerst bijwerken met `pull-all.sh`). Rapporteer per pad welke waarden geweigerd zouden worden. Verwacht: nul, behalve wat de profielen uit taak 3 al uitzonderen. Vindt de dry-run iets, dan gaat het eerst langs een mens. De meting van 14-10 staat bovenaan; die geeft de verwachting, de dry-run levert het bewijs. Deze stap draait lokaal, want de projects-repo staat niet op de orch-server.

## Buiten scope, benoemd

- **De service-config-routes ook door de editables** (PUT/PATCH uit `_register_service_config_routes` in `api/v2/router.py`). Pydantic blijft daar de vorm en het type toetsen; daar is niets mis mee. Wat ontbreekt is dat de waarden daarna nog door de editables gaan, want die doen meer dan toetsen: profiel, validator, converter. Dit wordt een eigen plan, omdat het een ontwerpvraag heeft die dit plan niet heeft:

  De API levert de **opslagvorm** aan, en de editables werken op de **formuliervorm**. Converters vertalen daartussen: `CommandLineConverter.read` maakt van de opgeslagen lijst één regel, `write` splitst hem weer. Een validator zoals `CommandLineValidator` verwacht die ene regel. Wie de opslagvorm rechtstreeks aan `validate` en `write` geeft, krijgt dus een verkeerde toets of een verminkte waarde. De juiste weg is een rondgang per editable uit `service.config_editables(layer)`: `converter.read` (opslag naar formulier), dan `prepare_field` uit taak 2, dan `converter.write` (terug naar opslag). Dat is dezelfde weg die een waarde via het formulier aflegt.

  De gate van dat plan: de rondgang moet de identiteit zijn op elke schone waarde. Meet dat over alle `config_editables` van de 19 services met config-editables, tegen de projects-repo. Een converter die niet verliesvrij heen en terug gaat (versleutelde waarden zijn de eerste kandidaat, zie `keep_existing_ciphertext_if_unchanged`) moet je eerst vinden, anders herschrijft elke API-call het projectbestand. Of section-enforcers ook mee moeten, is een vraag voor dat plan: ze hangen aan `FormSection`, de visualizerlaag, en niet aan de editable.

  Tot dat plan er is vangt de backstop uit taak 6 de gevaarlijke tekens op die routes af. `prepare_field` uit taak 2 is de bouwsteen: houd hem dus vrij van alles wat alleen het formulier kent.
- **Alle 510 kale `{{ ... }}` in `manifests/`.** Het grootste deel zijn systeemnamen die al als Kubernetes-naam gevalideerd zijn. Een guard die `tojson` afdwingt met een allowlist is waardevol, maar ook groot. Taak 7 doet alleen de twee plekken met gebruikersinvoer.
- **Approvals** lopen als ruwe body zonder processor, alleen voor platformbeheerders. De backstop dekt de tekens. De `APPROVAL_*`-editables erop afdwingen is een eigen klus.
- **Een e-mailbibliotheek** (`email-validator`). De bestaande regex met `fullmatch` en een maximum van 254 tekens is genoeg zolang we niet op aflevering toetsen.

## Bronnen

- Django `CharField.strip`: https://docs.djangoproject.com/en/5.2/ref/forms/fields/#charfield
- Django `ProhibitNullCharactersValidator`: https://docs.djangoproject.com/en/5.2/ref/validators/#prohibitnullcharactersvalidator
- Symfony `trim`: https://symfony.com/doc/current/reference/forms/types/text.html#trim, en voor `PasswordType`: https://symfony.com/doc/current/reference/forms/types/password.html#trim
- Laravel `TrimStrings`: https://laravel.com/docs/12.x/requests#input-trimming-and-normalization
- Rails `normalizes`: https://api.rubyonrails.org/v7.1/classes/ActiveRecord/Normalization/ClassMethods.html
- Pydantic validators: https://docs.pydantic.dev/latest/concepts/validators/
- OWASP Input Validation: https://cheatsheetseries.owasp.org/cheatsheets/Input_Validation_Cheat_Sheet.html
- OWASP XSS Prevention: https://cheatsheetseries.owasp.org/cheatsheets/Cross_Site_Scripting_Prevention_Cheat_Sheet.html
- OWASP e-mailvalidatie: https://cheatsheetseries.owasp.org/cheatsheets/Email_Validation_and_Verification_Cheat_Sheet.html
- CWE-180, valideren vóór canonicaliseren: https://cwe.mitre.org/data/definitions/180.html
- CWE-88, argumentinjectie: https://cwe.mitre.org/data/definitions/88.html
- Trojan Source (bidi): https://trojansource.codes/
- Unicode-normalisatie: https://unicode.org/reports/tr15/; confusables: https://www.unicode.org/reports/tr39/
- ReDoS: https://owasp.org/www-community/attacks/Regular_expression_Denial_of_Service_-_ReDoS
- Kubernetes-namen: https://kubernetes.io/docs/concepts/overview/working-with-objects/names/
