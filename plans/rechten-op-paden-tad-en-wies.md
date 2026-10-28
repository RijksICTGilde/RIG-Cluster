# Rollen, rechten en audit in ZAD

Status: **contract, 28 oktober 2026.** De besluiten in paragraaf 3 zijn genomen. Er is nog niets gebouwd. Gemeten op `main` `c65e6a3e0`.

## 0. Hoe je dit leest

Dit plan is zelfstandig. Alles wat je nodig hebt om het te bouwen staat hier of in deze repo. Het model is overgenomen uit twee andere projecten van dezelfde eigenaar (Wies en AMT); de delen die we overnemen staan uitgeschreven in paragraaf 5 en in de bijlagen A en B. De bronnen zijn openbaar, maar je hebt ze niet nodig:

- Wies: https://github.com/RijksICTGilde/wies, commit `3bfc7bfbc612848c66de2ea2f9984f19a1975412`, bestanden `wies/core/permission_engine.py`, `wies/core/permissions.py`, `wies/core/services/role_matrix.py`, `wies/core/inline_edit/audit.py`.
- AMT (repo heet `tad` lokaal): https://github.com/MinBZK/amt, commit `ebd37f6aa0248b8c2d478a5de5b9ed41412999e3`, bestanden `amt/core/authorization.py`, `amt/api/decorators.py`.

Verwante documenten in deze repo, lees ze als je bij dat deel bent:

- `features/futures/rechten-plan-van-aanpak.md` (RC-150): de oorspronkelijke aanbeveling. Dit plan voert fase 1 tot en met 4 daarvan uit, met de aanpassingen hieronder.
- `features/leden-en-api-tokens.md` (RC-322): leden, service accounts en tokens zoals ze nu werken.
- `features/otap-markering.md`: de markering `environment` op een deployment.
- `features/otp-op-de-zad-gebruiker.md`, `features/omgevingsvariabelen-achter-step-up.md` (RC-332, RC-388): de step-up.
- `features/futures/gebeurtenissen-vastleggen-en-melden.md`, punt 6 en 7: het datamodel en de bewaartermijn waar de audit op aansluit.
- `instructions/services.md`: het service-systeem.

Namen in paragraaf 3 en 4 (rollen, actions) zijn besloten. Namen van code-objecten in paragraaf 5 tot en met 7 zijn een voorstel: je mag afwijken als je in de PR zegt waarom.

## 1. Doel

Eén plek die zegt wie wat mag in ZAD, die zowel API als portaal afdwingt, die als rolmatrix te lezen is, en die vastlegt wie wat deed.

**Klaar is:** elke route draagt een action; één functie beslist voor API en portaal; de rolmatrix is een pagina die uit dezelfde regels volgt; elke schrijfactie en elke weigering staat als rij in de audit-tabel; en de rollen uit paragraaf 4 gelden.

## 2. De uitgangssituatie

Gemeten, met de plekken waar je het vindt (paden relatief aan `operations-manager/python/`).

**Rollen.** Het schema (`opi/schemas/project_v2.json`, `users[].role`) kent `admin`, `owner`, `member`, `developer`. In de projects-repo staat 138 keer `role: admin` en geen andere rol. De handhaving:

| rol | portaal | API-token |
|---|---|---|
| `admin`, `owner` | alles | alles |
| `developer` | lezen, plus job, databaseconsole, restore | alleen lezen |
| `member` | lezen | alleen lezen |

Een platformbeheerder krijgt op elk project `admin` (`get_user_role_for_project` in `opi/services/project_authorization.py:78`).

**Twee beslissers die niet samenwerken.**

- API: `opi/core/handelingen.py` (sinds RC-322, 24 oktober) heeft de enum `Handeling` met ongeveer 45 waarden en de decorator `@handeling(...)`, op 72 routes. `opi/api/principaal.py` heeft `ROL_HANDELINGEN` (rol naar handelingen), `MENS_VERPLICHT` (alleen een mens) en `SCHRIJFROLLEN`.
- Portaal: de tuples `PROJECT_EDIT_ROLES` en `PROJECT_OPERATE_ROLES` in `opi/services/project_authorization.py`, de helpers `require_project_edit_access` en `require_project_operate_access` in `opi/web/project_edit_security.py`, en ongeveer twintig losse `["admin", "owner"]`-controles in `opi/web/router.py` en in templates (`bg/_deployment-actions.html.j2`, `bg/_pending-rollout.html.j2`, `bg/project-tabs.html.j2`).
- Ongeveer 218 routes in totaal (121 in `opi/web`, 97 in `opi/api`).

**Step-up.** Per route met `@requires_step_up(predicaat, actie=StepUpActie.X, velden=(...))` (`opi/core/auth_decorators.py:100`). De predicaten staan in `opi/services/step_up_conditions.py`, de enum in `opi/core/step_up.py`, de API-kant in `opi/api/otp_step_up.py`. `tests/test_handelingen.py` legt vast welke handeling bij welke step-up hoort.

**Rollen schrijven, twee wegen.**

1. De Team-kaart en de API (`opi/web/router_tokens.py`, `opi/api/token_router.py`): rol wijzigen, service account toevoegen, token uitgeven. Achter een step-up.
2. De team-wizard (de flow die `users` bezit, `opi/web/router_detail_edit.py:765`, `opi/forms/wizard/leden.py`). De rol is daar een **verborgen veld met standaard `admin`** (`opi/forms/visualizers/fields/team.py`, `opi/forms/editables/fields/team.py`). Wie een lid toevoegt maakt dus altijd een admin, zonder step-up, en een gemanipuleerde POST kan elke rol zetten. Dit verklaart de 138 admins.

**Wie deed wat.** `Actor` (`opi/core/actor.py`) staat als trailer op elke commit van een projectbestand, en in `async_tasks.created_by`. Geen doorzoekbare lijst, en weigeringen worden niet vastgelegd.

## 3. Besluiten

| # | besluit |
|---|---|
| B1 | Een lid heeft een **lijst rollen**: `roles: [admin, ops]`. Admin is gebruikersbeheer, geen bovenste trede. |
| B2 | `owner` en `member` verdwijnen. |
| B3 | `developer` werkt op alles wat niet productie is: deployments aanmaken, wijzigen, verwijderen; componenten; services; env-vars (met step-up); job, databaseconsole, restore. |
| B4 | `ops` mag wat `developer` mag, ook op productie, en zet de OTAP-markering. |
| B5 | Een wijziging op projectniveau (service, component) vraagt `ops` zodra één deployment van het project op productie staat. |
| B6 | Bestaande admins krijgen `[admin, ops]`. Een platformbeheerder krijgt op elk project `[admin, ops]`, zoals hij nu `admin` krijgt. |
| B7 | Twee CI-rollen die niet op elkaar stapelen. `ops-ci`: image vervangen op elke deployment, ook productie, en verder niets. `developer-ci`: deployments die niet productie zijn upserten, verwijderen, en daar het image vervangen. |
| B8 | Geen guard die CI-rollen aan service accounts bindt. Voor nu. |
| B9 | Step-up is een voorwaarde **per grant**. |
| B10 | Code-identifiers en opgeslagen of verstuurde waarden worden Engels: `Handeling`, `StepUpActie`, de `cause`-waarden, de nieuwe engine. Commentaar en docstrings blijven Nederlands met Engelse vaktermen, zoals CLAUDE.md voorschrijft. |
| B11 | Audit sluit aan op het datamodel `notification_events` uit `features/futures/gebeurtenissen-vastleggen-en-melden.md` punt 7. Geen tweede tabel. |
| B12 | Een admin kiest en wijzigt in de UI welke rol of rollen een lid heeft, bij het toevoegen en daarna. Bij het toevoegen staat `[developer]` vooraf gekozen; dat is een voorinvulling, geen vaste toekenning. In de API kan een admin de rollen van een bestaand lid wijzigen; een mens toevoegen via de API valt buiten dit plan (paragraaf 9). |
| B13 | De **sleutel** van een rol (`developer`) is wat in het projectbestand, de API en de audit staat. De **weergave** (`Ontwikkelaar`) is een label op één plek in code, zodat een andere naam op het scherm geen migratie is. Elke plek die een rol aan een mens toont, gebruikt het label. |

## 4. De rollen en de matrix

| rol | omvat | waarvoor |
|---|---|---|
| `viewer` | | lezen, behalve env-var-waarden en secrets |
| `developer` | `viewer` | bouwen op alles wat niet productie is |
| `ops` | `developer` | hetzelfde op productie, en de OTAP-markering |
| `admin` | `viewer` | leden, rollen, tokens, service accounts, project verwijderen |
| `developer-ci` | | deployments niet-productie, voor een pijplijn |
| `ops-ci` | | image vervangen op elke deployment, voor een pijplijn |

De sleutels hierboven zijn besloten. Het label op het scherm is los te wijzigen (B13); beginwaarden staan in 5.3.

Spelregel: een nieuwe rol komt er alleen als er een echte houder is die een strikte deelverzameling nodig heeft, en de reden staat bij de rol in het regelbestand.

`✓` mag. `✓ s` mag met verse step-up. Leeg mag niet. Een service account kan nooit een step-up geven, dus `✓ s` is voor hem dicht.

| handeling | viewer | developer | ops | admin | developer-ci | ops-ci |
|---|---|---|---|---|---|---|
| project, deployments, logs, taken, back-ups lezen | ✓ | ✓ | ✓ | ✓ | | |
| deployment aanmaken of wijzigen, niet-productie | | ✓ | ✓ | | ✓ | |
| deployment aanmaken of wijzigen, productie | | | ✓ | | | |
| deployment verwijderen, niet-productie | | ✓ | ✓ | | ✓ | |
| deployment verwijderen, productie | | | ✓ s | | | |
| image vervangen, niet-productie | | ✓ | ✓ | | ✓ | ✓ |
| image vervangen, productie | | | ✓ | | | ✓ |
| env-vars bekijken en wijzigen, niet-productie | | ✓ s | ✓ s | | | |
| env-vars bekijken en wijzigen, productie | | | ✓ s | | | |
| service of component op projectniveau, project zonder productie | | ✓ | ✓ | | | |
| service of component op projectniveau, project met productie | | | ✓ | | | |
| component verwijderen, niet in productie | | ✓ | ✓ | | | |
| component verwijderen, in productie (`component_in_production`) | | | ✓ s | | | |
| job, databaseconsole, restore, niet-productie | | ✓ | ✓ | | | |
| job, databaseconsole, restore, productie | | | ✓ | | | |
| OTAP-markering zetten of verlagen | | | ✓ s | | | |
| leden toevoegen, verwijderen, rol wijzigen | | | | ✓ s | | |
| tokens en service accounts beheren | | | | ✓ s | | |
| project verwijderen | | | | ✓ s | | |

Deze tabel is de toets voor stap 7: de gegenereerde matrix moet hem opleveren. Wat het project verlaat (eigen domein, resources boven het quotum, iets publiek maken) blijft goedkeuring via `ApprovalSpec` (`opi/services/catalog/approval.py`). Dat is geen rolrecht en valt buiten dit plan.

De markering staat bij `ops` met step-up, omdat wie de markering mag verlagen elke productieregel met één klik omzeilt.

## 5. Het model

### 5.1 Wat we uit Wies overnemen, en wat anders moet

Wies (bijlage A) declareert elke regel als `rule(verb, target, label=..., grants=[Grant(holder, scope)], requires=...)`. Alle regels staan in één bestand dat je van boven naar beneden leest als de rechtenmatrix. De engine voert die declaratie uit, en de matrixpagina leest dezelfde declaratie: één rij per (regel, scope), één kolom per rol. Grants binnen een regel tellen op. Een scope heeft `parts`, en een grant dekt een rij als zijn parts een deelverzameling zijn van die van de rij: wie andermans object mag wijzigen, mag ook het zijne. Registreren faalt luid als het label ontbreekt of als een scope niet in `SCOPES` staat.

Aanpassingen voor ZAD:

1. **Target is een `Action`**, geen modelklasse. ZAD heeft geen ORM-modellen voor zijn objecten; het projectbestand is de waarheid.
2. **Holder is een projectrol**, met een omvat-tabel. In Wies noemt elke regel elke rol; met stapeling noemt een regel alleen de laagste rol die volstaat.
3. **Een scope is een predicaat op de resource** (markering van de deployment, of het project productie heeft), niet op de relatie van de gebruiker met een rij.
4. **Een grant draagt `requires`**: `STEP_UP` en `HUMAN_ONLY`. Wies kent geen step-up.
5. **De beslisfunctie geeft een reden terug**, niet alleen een boolean, omdat het portaal op een ontbrekende step-up de dialoog moet openen.

### 5.2 Wat we uit AMT overnemen

AMT (bijlage B) annoteert een route met `@permission({"organization/{organization_id}/algorithm": [Verb.READ]})` en vult het sjabloon met de route-kwargs. **Wel:** de resource als pad, ingevuld uit de route-kwargs (`project/{project_name}/deployment/{deployment_name}`), voor de audit en voor de scope. **Niet:** de exacte sleutelvergelijking op een unie (een specifieker pad wordt dan een extra recht, dus toevoegen kan nooit inperken), en een databaseopzoeking in de decorator. In ZAD is dat één ProjectStore-lezing die de beslissing in gaat.

### 5.3 De vorm in ZAD (voorstel)

Nieuw pakket `opi/core/authz/` met drie modules: `engine.py` (de woordenlijst en de uitvoering), `rules.py` (alle regels, het bestand dat je leest), `matrix.py` (de matrix uit de regels).

```python
# engine.py
class Role(StrEnum):
    VIEWER = "viewer"
    DEVELOPER = "developer"
    OPS = "ops"
    ADMIN = "admin"
    DEVELOPER_CI = "developer-ci"
    OPS_CI = "ops-ci"

# Wat een mens ziet. Hernoemen is een regel hier, geen migratie (B13). Beginwaarden: alleen
# developer is besloten; de rest is de sleutel tot iemand een andere naam kiest.
ROLE_LABELS: dict[Role, str] = {
    Role.VIEWER: "Viewer",
    Role.DEVELOPER: "Ontwikkelaar",
    Role.OPS: "Ops",
    Role.ADMIN: "Admin",
    Role.DEVELOPER_CI: "Developer CI",
    Role.OPS_CI: "Ops CI",
}
# Een zin per rol voor de Team-kaart en de matrix: wat de rol is en waarom hij bestaat (de
# spelregel uit paragraaf 4).
ROLE_DESCRIPTIONS: dict[Role, str] = {...}

def role_label(role: Role) -> str: ...  # een opgeslagen sleutel zonder label toont zichzelf

# Stapeling op een plek. Transitief: ops omvat developer omvat viewer.
INCLUDES: dict[Role, frozenset[Role]] = {
    Role.DEVELOPER: frozenset({Role.VIEWER}),
    Role.OPS: frozenset({Role.DEVELOPER}),
    Role.ADMIN: frozenset({Role.VIEWER}),
}

def effective_roles(roles: Iterable[Role]) -> frozenset[Role]: ...  # transitieve sluiting

@dataclass(frozen=True)
class Resource:
    project: str
    path: str                          # "project/demo/deployment/live"
    deployment: str | None = None
    environment: str | None = None     # markering van deze deployment, standaard "ontwikkeling"
    component: str | None = None
    component_in_production: bool = False
    project_has_production: bool = False

@dataclass(frozen=True)
class Scope:
    name: str
    label: str
    predicate: Callable[[Resource], bool]

ANY = Scope("any", "", lambda r: True)
PRODUCTION = Scope("production", "productie", lambda r: r.environment == "productie")
NON_PRODUCTION = Scope("non-production", "niet-productie", lambda r: r.environment != "productie")
PROJECT_WITH_PRODUCTION = Scope("project-with-production", "project met productie", lambda r: r.project_has_production)
PROJECT_WITHOUT_PRODUCTION = Scope("project-without-production", "project zonder productie", lambda r: not r.project_has_production)
# Een component is in productie als een productiedeployment hem gebruikt; zelfde betekenis als
# het huidige predicaat component_in_production in opi/services/step_up_conditions.py.
COMPONENT_IN_PRODUCTION = Scope(...)
COMPONENT_NOT_IN_PRODUCTION = Scope(...)
SCOPES = (ANY, NON_PRODUCTION, PRODUCTION, PROJECT_WITHOUT_PRODUCTION, PROJECT_WITH_PRODUCTION,
          COMPONENT_NOT_IN_PRODUCTION, COMPONENT_IN_PRODUCTION)

class Requirement(StrEnum):
    HUMAN_ONLY = "human-only"   # de soort aanroeper; nu MENS_VERPLICHT
    STEP_UP = "step-up"         # een verse tweede factor; impliceert HUMAN_ONLY

@dataclass(frozen=True)
class Grant:
    role: Role
    scope: Scope = ANY
    requires: frozenset[Requirement] = frozenset()

@dataclass(frozen=True)
class Rule:
    action: Action
    label: str
    section: str
    grants: tuple[Grant, ...]

def rule(action: Action, *, label: str, section: str, grants: Iterable[Grant]) -> Rule: ...
# Faalt bij: een tweede regel voor dezelfde action, een leeg label, een scope buiten SCOPES.
# Lege grants mag: dan mag niemand het, en de matrix toont dat.

class Cause(StrEnum):
    INSUFFICIENT_ROLE = "insufficient-role"
    PRINCIPAL_KIND_NOT_ALLOWED = "principal-kind-not-allowed"
    STEP_UP_REQUIRED = "step-up-required"

@dataclass(frozen=True)
class Decision:
    allowed: bool
    cause: Cause | None = None
    grant: Grant | None = None

def decide(principal: Principal, action: Action, resource: Resource, *, step_up_fresh: bool) -> Decision: ...
```

De beslisregel van `decide`:

1. Kandidaten zijn de grants van de regel waarvan de rol in `effective_roles(principal.roles)` zit en waarvan de scope op de resource klopt.
2. Geen kandidaat: `INSUFFICIENT_ROLE`.
3. Een kandidaat waarvan alle voorwaarden kloppen: toegestaan, met die grant (de audit legt hem vast).
4. Anders, als de aanroeper geen mens is: `PRINCIPAL_KIND_NOT_ALLOWED`. Als hij een mens is zonder verse step-up: `STEP_UP_REQUIRED`.

Een action zonder regel is een fout bij het opstarten, niet een weigering bij het aanroepen.

```python
# rules.py, een paar regels als voorbeeld
rule(Action.DEPLOYMENT_DELETE, label="Deployment verwijderen", section="Deployments", grants=[
    Grant(Role.DEVELOPER, NON_PRODUCTION),
    Grant(Role.DEVELOPER_CI, NON_PRODUCTION),
    Grant(Role.OPS, PRODUCTION, requires=frozenset({Requirement.STEP_UP})),
])

rule(Action.DEPLOYMENT_IMAGE_REPLACE, label="Image vervangen", section="Deployments", grants=[
    Grant(Role.DEVELOPER, NON_PRODUCTION),
    Grant(Role.DEVELOPER_CI, NON_PRODUCTION),
    Grant(Role.OPS),
    Grant(Role.OPS_CI),  # bewust zonder step-up: anders kan een pijplijn het nooit
])

rule(Action.ENV_VARS_MANAGE, label="Env-vars bekijken en wijzigen", section="Deployments", grants=[
    Grant(Role.DEVELOPER, NON_PRODUCTION, requires=frozenset({Requirement.STEP_UP})),
    Grant(Role.OPS, PRODUCTION, requires=frozenset({Requirement.STEP_UP})),
])

rule(Action.TOKEN_MANAGE, label="Tokens en service accounts beheren", section="Leden", grants=[
    Grant(Role.ADMIN, requires=frozenset({Requirement.STEP_UP})),
])
```

`Principal` is het huidige `Principaal` uit `opi/api/principaal.py`, met `roles: frozenset[Role]` in plaats van `handelingen`. Er is één bouwer voor een principal uit een sessie (portaal) en één uit een token (API). Beide lezen de rollen bij elke beslissing opnieuw uit het projectbestand, zoals tokens dat nu al doen.

`Resource` wordt gebouwd uit de route-kwargs en één ProjectStore-lezing. Het padsjabloon hoort bij de action, VOORSTEL: een tweede argument op de decorator, `@action(Action.DEPLOYMENT_DELETE, resource="project/{project_name}/deployment/{deployment_name}")`. Zonder sjabloon is het `project/{project_name}`.

### 5.4 De matrix

`matrix.py` leest het register en maakt per regel een rij per scope die een van zijn grants noemt; een regel met alleen `ANY` krijgt één rij zonder scopelabel. Per rij een kolom per rol, met `role_label` als kop: `✓` als een grant met die rol (of een rol die hem omvat, via `INCLUDES`) die scope dekt, met de markering `s` als die grant `STEP_UP` vraagt. Een extra regel per rol of hij voor een service account bereikbaar is: een cel met `STEP_UP` of `HUMAN_ONLY` is dat niet.

Pagina: `/admin/roles` (VOORSTEL), voor platformbeheerders, plus een link vanaf de Team-kaart van een project. LOTC-tabel, geen eigen opmaak.

Bij het opstarten faalt de applicatie als:

- een route geen action draagt,
- een action geen regel heeft,
- een rol in `Role` in geen enkele grant voorkomt,
- een rol bestaat waarvan **elke** grant `STEP_UP` of `HUMAN_ONLY` vraagt, terwijl de rol op `-ci` eindigt (een CI-rol die niets kan).

### 5.5 Waar `decide` wordt aangeroepen

- **API**: het handhavingspunt in `opi/api/principaal.py` (nu `ROL_HANDELINGEN`-opzoeking) roept `decide`.
- **Portaal**: één dependency die de action van de route leest en `decide` roept. Die vervangt `require_project_edit_access`, `require_project_operate_access`, `_require_project_member_access` en de losse rolcontroles.
- **Templates**: een helper `can(action, deployment=None)` in de Jinja-context, die dezelfde `decide` roept zonder step-up-eis te tellen (een knop toont zich als de rol volstaat; de step-up volgt bij het klikken). Geen rolnaam meer in een template.
- **Step-up**: de mechaniek blijft (dialoog, venster van vijf minuten, replay, `X-ZAD-OTP`, `velden=`). `@requires_step_up` leest of er een step-up nodig is uit `decide` (`STEP_UP_REQUIRED`) in plaats van uit zijn eigen predicaat. De `StepUpAction` blijft voor de teksten in de dialoog.

## 6. Audit

### 6.1 Wat er wordt vastgelegd

Eén hook rond `decide`, zonder code per route. Hij schrijft een rij voor:

- elke toegestane aanroep van een route waarvan de action geen leesactie is,
- elke weigering, ook van een leesactie.

Een leesactie is een action die in de regels als zodanig gemarkeerd is (VOORSTEL: `rule(..., read=True)`), wat `LEESHANDELINGEN` vervangt.

| veld | bron |
|---|---|
| wie | `Actor` (`opi/core/actor.py`): soort en identiteit |
| wat | de action |
| waarop | `Resource.path`, plus project, deployment, markering |
| besluit | toegestaan, of de `Cause` |
| via welke grant | rol en scope van de grant die het toestond |
| step-up | of er een gebruikt is |
| uitkomst | de statuscode van het antwoord, en de taak-id als er een taak start |
| welke wijziging | de **commit-SHA** van het projectbestand als de aanroep er een maakte |
| oorsprong | client-IP, volgens de regel in de gebeurtenissendocumenten |

De commit-SHA is waarom dit zonder een if-then-else per route kan. Wies legt zelf voor en na vast (`audit_state`, bijlage A), omdat Django geen geschiedenis heeft. ZAD heeft git: `git show <sha>` geeft de diff, en er komen geen secrets in de audit. Waar de waarde niet in het projectbestand staat (welk image, welke back-up), noemt de route de velden, zoals `@requires_step_up(..., velden=(...))` dat nu al doet; alleen die velden gaan mee, nooit de hele body.

### 6.2 Waar het staat

De tabel `notification_events` uit `features/futures/gebeurtenissen-vastleggen-en-melden.md` punt 7, als Alembic-migratie in `opi/migrations/versions/` en met een model in `opi/services/persistence/`, zoals `async_tasks` en `users`. De audit is daar de eerste schrijver. Twee soorten (VOORSTEL): `nl.rig.zad.action.performed.v1` en `nl.rig.zad.action.denied.v1`, met de action, grant, cause, step-up, statuscode en commit-SHA in `data`. `actor`, `actor_kind`, `origin`, `project`, `deployment`, `task_id` op hun eigen kolommen. `subject` is `<project>` of `<project>/<deployment>`, nooit een e-mailadres.

Alleen invoegen. De bewaartermijn uit punt 6 van dat document geldt: na 90 dagen worden `actor` en `origin` op `NULL` gezet, na een jaar gaat de rij weg. Die lus hoort bij deze stap, want zonder lus bewaart de tabel persoonsgegevens zonder termijn.

Wat de gebeurtenissendocumenten verder beschrijven (postvak, ontvangers, push-kanalen, CloudEvents-export, de OIN) valt buiten dit plan.

### 6.3 Wat je ziet

Op de projectpagina een tabblad met de audit van dat project, voor `admin`. Platformbeheerders zien alles. Leesrechten volgen een eigen regel `Action.AUDIT_READ`.

## 7. Schrijfwegen en hun guards

Wat wordt schrijfbaar dat het niet was, en welke guard staat erop:

| schrijfweg | nu | na dit plan |
|---|---|---|
| `roles` via de Team-kaart en `token_router` | rol, achter step-up | `Action.MEMBER_MANAGE`, admin met step-up. De API-route `PUT .../leden/{naam}/rol` neemt `roles: [...]` in plaats van `rol`. |
| `roles` via de team-wizard | verborgen veld, standaard `admin`, geen step-up, POST kan elke rol zetten | de wizard toont per lid een **meerkeuze** met de rollen (met `role_label`), vooraf `[developer]` voor een nieuw lid (B12). Opslaan van de team-sectie vraagt `MEMBER_MANAGE` met step-up. |
| beide wegen | twee eigen implementaties | **één** servicefunctie (VOORSTEL `set_member_roles`) die beide wegen aanroepen. Hij weigert een onbekende rol, een lege lijst, en een wijziging die het project zonder `admin` laat (ook als een admin zichzelf degradeert). Tokens en service accounts komen terug zoals `zet_verborgen_leden_terug` dat nu doet. |
| `users` via een andere wizard-flow | niet afgeschermd door `apply_form_data_to_project` | een flow die `users` niet bezit en het toch indient, wordt geweigerd, met een test |
| `environment` op een deployment | verlagen achter step-up (`MARKERING_VERLAGEN`) | zetten en verlagen vragen `ops` met step-up |
| de audit-tabel | bestaat niet | alleen de hook schrijft; geen route, geen API |
| `roles` via de schemamigratie | | eenmalig, zie stap 7 |

## 8. Fasering

Drie orch-taken. Taak A en B veranderen geen gedrag. Taak C wel.

### Taak A: woordenlijst, register, matrix, elke route een action

**A1. Woordenlijst naar het Engels.** `opi/core/handelingen.py` wordt `opi/core/authz/actions.py` met `Action`, en de decorator heet `@action`. `StepUpActie` wordt `StepUpAction`. `Principaal`, `PrincipaalSoort`, `ROL_HANDELINGEN`, `LEESHANDELINGEN`, `MENS_VERPLICHT`, `SCHRIJFROLLEN`, `NietHerkend`, `ToegangGeweigerd` krijgen Engelse namen. Andere Nederlandse namen buiten de authz-modules (`project_leden`, `LidSoort`, routes als `/leden`) blijven staan; die omzetten is niet gevraagd.

Waarden, besloten:

| oud | nieuw |
|---|---|
| `project.lezen`, `.lijsten`, `.aanmaken`, `.verwijderen`, `.verversen` | `project.read`, `.list`, `.create`, `.delete`, `.refresh` |
| `deployment.lezen`, `.schrijven`, `.verwijderen`, `.verversen`, `.image-vervangen` | `deployment.read`, `.write`, `.delete`, `.refresh`, `.image-replace` |
| `component.lezen`, `.toevoegen`, `.wijzigen`, `.verwijderen`, `.koppelen` | `component.read`, `.add`, `.update`, `.delete`, `.link` |
| `dienst.lezen`, `.toevoegen`, `.configureren`, `.waarden-schrijven`, `.actie` | `service.read`, `.add`, `.configure`, `.write-values`, `.action` |
| `database.klonen`, `.schema-lezen`, `.schema-beheren` | `database.clone`, `.schema-read`, `.schema-manage` |
| `bucket.klonen`, `kloon.valideren` | `bucket.clone`, `clone.validate` |
| `registry.toevoegen`, `image.pushen` | `registry.add`, `image.push` |
| `backup.lezen`, `.maken`, `.verwijderen` | `backup.read`, `.create`, `.delete` |
| `herstel.lezen`, `herstel.uitvoeren` | `restore.read`, `restore.run` |
| `logs.lezen`, `resources.bijstellen`, `resources.opschonen` | `logs.read`, `resources.adjust`, `resources.cleanup` |
| `subdomein.lezen`, `cluster.lezen` | `subdomain.read`, `cluster.read` |
| `taak.lezen`, `taak.annuleren` | `task.read`, `task.cancel` |
| `slaapstand.lezen`, `slaapstand.wekken` | `sleep.read`, `sleep.wake` |
| `projectsleutel.lezen`, `token.lezen`, `token.beheren` | `project-key.read`, `token.read`, `token.manage` |

`StepUpAction`: `project-delete`, `deployment-delete`, `component-delete`, `marking-lower`, `cleanup-start`, `reconciliation-start`, `marking-revoke`, `orphans-confirm`, `projects-reload`, `project-key-replace`, `token-issue`, `token-rotate`, `token-revoke`, `role-change`, `service-account-add`, `service-account-remove`, `env-vars-manage`.

`cause`: `te-weinig-rechten` wordt `insufficient-role`, `tokensoort-niet-toegestaan` wordt `principal-kind-not-allowed`, `otp-ontbreekt` wordt `otp-missing`, `code-ongeldig` wordt `code-invalid`, `factor-ontbreekt` wordt `factor-missing`.

De `cause` en de step-up-`action` staan in API-antwoorden. Werk `features/leden-en-api-tokens.md`, `features/otp-op-de-zad-gebruiker.md`, `features/projectsleutel-vervangen.md` en `features/platformbeheer-via-de-api.md` bij. Er is in deze repo geen client die erop stuurt.

*Controle:* alle tests groen; `grep -rn "Handeling\|StepUpActie\|Principaal" opi tests` levert niets op.

**A2. Het register** uit paragraaf 5.3, dat de **huidige** rechten beschrijft, nog niet die van paragraaf 4. Voor nu is de rol een tijdelijke vertaling van de huidige enum: `admin`/`owner` worden `{admin, ops}`, `developer` wordt de huidige operate-set, `member` wordt `viewer`. `ROL_HANDELINGEN` en `MENS_VERPLICHT` verdwijnen en volgen uit de regels.

*Controle:* een test zet voor elke combinatie van (huidige rol, soort aanroeper, action, markering) de uitkomst van `decide` naast de huidige uitkomst van de API-handhaving en de portaal-tuples, en ze zijn gelijk. Een test toont dat de step-ups per route gelijk zijn aan de huidige `@requires_step_up`-predicaten.

**A3. De matrixpagina** uit 5.4.

*Controle:* elke regel staat op de pagina; een regel zonder label of met een onbekende scope start niet op.

**A4. Elke route een action**, ook in `opi/web`. Nieuwe actions die je nodig hebt volgen het patroon `<object>.<werkwoord>` in het Engels, kebab-case, en staan in de PR. Er zijn er minstens bij nodig: `env-vars.manage`, `member.manage`, `environment.set`, `job.run`, `database.console`, `audit.read`. Platformroutes (achter `is_platform_admin`) krijgen een action onder `platform.*` en een regel met een eigen holder `PLATFORM_ADMIN`; hun handhaving verandert niet.

*Controle:* de opstartcontrole uit 5.4 staat aan en de applicatie start; een test bewijst dat een route zonder action de opstart laat falen.

### Taak B: audit

**B1.** De tabel, het model en de migratie uit 6.2. **B2.** De hook rond `decide` uit 6.1, inclusief de commit-SHA en de taak-id. **B3.** De bewaarlus (90 dagen, een jaar) als geplande taak naast de bestaande in `opi/core/simple_background.py` of de scheduler die daar hoort. **B4.** Het audit-tabblad uit 6.3.

*Controle:* een test die door elke route met een schrijfactie loopt en per aanroep precies één rij verwacht, met de juiste actor, action, resource en uitkomst; een geweigerde aanroep geeft ook een rij, met de cause; de bewaarlus zet na 90 dagen `actor` en `origin` op `NULL` en laat de rest staan; geen rij bevat een e-mailadres buiten `actor`.

### Taak C: de handhaving om, en de nieuwe rollen

**C1. Portaal in schaduw.** De portaal-dependency uit 5.5 draait naast de bestaande helpers en logt elk verschil als warning. Daarna per helper omzetten: eerst `require_project_edit_access`, dan `_require_project_member_access`, dan `require_project_operate_access`, dan de losse controles in `opi/web/router.py`, als laatste de templates (via `can`). `@requires_step_up` leest zijn beslissing uit `decide`.

*Controle:* per omgezette helper een test dat de uitkomst gelijk is aan die van A2; na de omzetting `grep -rn '"owner"' opi` levert alleen nog de migratie op.

**C2. Schemamigratie naar 2.13** in `opi/services/schema_migration.py`, met een legacy-patch in `opi/schemas/project_legacy/`. `users[].role: x` wordt `users[].roles: [...]`: `admin` en `owner` worden `[admin, ops]`, `developer` blijft `[developer]`, `member` wordt `[viewer]`. Het schema accepteert alleen de zes rollen uit paragraaf 4, als lijst met minstens één waarde. Een project heeft minstens één lid met `admin`; het schema of de validatie weigert anders.

*Controle:* draai de migratie op een kopie van de projects-repo (lokaal: `~/IdeaProjects/rig-cluster-test-git-repositories/rig-cluster-projects-github`, niet pushen). Elk lid dat `admin` was, heeft daarna `[admin, ops]`. Nergens staat nog `role:`. Elk bestand valideert.

**C3. De regels van paragraaf 4.** Vervang de tijdelijke vertaling uit A2 door de regels die paragraaf 4 beschrijft. Dit is de gedragswijziging. Platformbeheerders krijgen op elk project `[admin, ops]` (B6).

*Controle:* een test die de gegenereerde matrix vergelijkt met de tabel in paragraaf 4, cel voor cel.

**C4. De schrijfwegen uit paragraaf 7.** De team-wizard en de Team-kaart schrijven rollen allebei via `set_member_roles`; opslaan vraagt `member.manage` met step-up; andere flows mogen `users` niet indienen; de markering vraagt `ops`.

*Controle:* een developer die de POST van de team-wizard of de rolroute stuurt krijgt 403 en het bestand verandert niet; een admin zonder verse step-up krijgt `step-up-required`; een onbekende rol, een lege lijst en het weghalen van de laatste admin worden geweigerd, via beide wegen; een test dat een developer de markering van een productiedeployment niet kan verlagen of zetten.

**C5. De Team-kaart en de team-wizard** tonen per lid de rollen als meerkeuze, met `role_label` en `ROLE_DESCRIPTIONS`, achter de bestaande step-up. De waarde die het formulier verstuurt is de sleutel. De keuzelijst `UserRoleOptionsProvider` (`opi/forms/visualizers/providers.py:306`, nu met de ongeldige rol `operator`) leest uit hetzelfde register of verdwijnt.

*Controle:* een test die `ROLE_LABELS[Role.DEVELOPER]` in de test wijzigt en ziet dat Team-kaart en matrix het nieuwe label tonen, terwijl het projectbestand `developer` houdt; `grep` op een rolsleutel in `opi/templates_lotc` levert geen getoonde tekst op.

## 9. Buiten dit plan

- **Productie en ontwikkeling in één projectbestand.** B5 maakt zichtbaar dat dat onhandig is: in een project met productie kan een developer geen service meer wijzigen.
- **Platformbeheerder als grant met vervaltijd**, in plaats van `is_platform_admin()`.
- **`ApproverScope`** (`platform-admin`, `project-admin`, `project-member`) samenvoegen met de rollen. Nodig voordat er veldrechten komen.
- **Een mens toevoegen via de API.** Service accounts kunnen dat al (`POST .../leden`); mensen alleen via de UI. Als het komt, roept het `set_member_roles` aan.
- **Veldrechten**, en **pad-scope op een grant** (een token alleen voor deployment X).
- **De lijstvraag**: de projectenlijst filtert nu op lidmaatschap, en dat volstaat zolang rechten per project zijn.

## Bijlage A: de engine van Wies

Ingekort uit `wies/core/permission_engine.py` en `wies/core/permissions.py` (commit hierboven). Het laat de vorm zien die we overnemen. De ZAD-vorm in 5.3 wijkt af waar 5.1 dat zegt.

```python
class Verb(StrEnum):
    READ = "read"
    UPDATE = "update"
    DELETE = "delete"

@dataclass(frozen=True)
class Scope:
    """De relatie die een houder met het object moet hebben."""
    name: str
    label: str
    predicate: Callable[[object, object, object], bool]   # (user, obj, grant)
    parts: frozenset[str] | None = None                    # standaard {name}

ANY = Scope("any", "van een ander", lambda _u, _o, _g: True, frozenset())
OWN = Scope("own", "eigen", _is_assignment_owner)
SELF = Scope("self", "je eigen", _is_self)
SCOPES = (OWN, SELF, PLACED, PLACED_ON_SERVICE, ANY)      # smalste eerst

@dataclass(frozen=True)
class Role:
    group: str
    def qualifies(self, user, obj, verb) -> bool:
        return user.groups.filter(name=self.group).exists()

@dataclass(frozen=True)
class Grant:
    holder: Role | Anyone
    scope: Scope = ANY
    def holds(self, user, obj, verb) -> bool:
        return self.scope.predicate(user, obj, self) and self.holder.qualifies(user, obj, verb)

@dataclass(frozen=True)
class Rule:
    verb: Verb
    model: type
    field_name: str | None
    label: str
    grants: tuple[Grant, ...]
    requires: tuple[Condition, ...] = ()
    def __call__(self, user, obj) -> bool:
        if not all(c.holds(obj) for c in self.requires):
            return False
        return any(g.holds(user, obj, self.verb) for g in self.grants)

_RULES: dict[tuple[Verb, type, str | None], Rule] = {}

def rule(verb, target, *, label, grants, requires=()) -> Rule:
    # label en grants zijn verplicht, zodat een recht niet kan bestaan zonder rij in de matrix.
    # Lege grants: niemand mag het. Een scope buiten SCOPES is een fout.
    # target is een modelklasse (heel object) of een Editable (veld).
    ...

def has_permission(verbs, obj, user, field=None) -> bool:
    # Zoekt (verb, model, veld), valt terug op (verb, model, None). Meerdere verbs: OF.
    ...
```

Een regel in `permissions.py`:

```python
rule(UPDATE, Colleague, label="Collega wijzigen", grants=[
    Grant(Role(ROLE_USER_ADMIN)),
    Grant(Anyone(), SELF),
])

# Niemand: een adres wijzig je op het gebruikersformulier.
rule(UPDATE, UserEditables.email, label="E-mailadres wijzigen met het potlood", grants=[])
```

De matrix (`wies/core/services/role_matrix.py`) leest het register zonder iets uit te voeren:

```python
def row_scopes(rule) -> set:
    return {ANY} | {grant.scope for grant in rule.grants}

def _rule_cell(rule, scope, user) -> bool:
    # Een grant dekt een rij als zijn relatie niet meer vraagt dan die van de rij.
    return any(grant.scope.parts <= scope.parts and grant.holder.qualifies(user, None, rule.verb)
               for grant in rule.grants)
```

De audit van Wies (`wies/core/inline_edit/audit.py`) schrijft per gewijzigd veld een `Event` met `user`, `object_type`, `action`, `object_id`, `ip`, `user_agent` en `context = {field_name, old_value, new_value}`. Het event komt uit de laag die het veld wijzigt, niet uit de view, zodat elk scherm dezelfde events schrijft. ZAD neemt het principe over (één laag schrijft, niet elke route) maar legt de commit-SHA vast in plaats van de oude en nieuwe waarde (6.1).

## Bijlage B: de annotatie van AMT

Ingekort uit `amt/api/decorators.py` en `amt/core/authorization.py`.

```python
class AuthorizationVerb(StrEnum):
    LIST = "List"; READ = "Read"; CREATE = "Create"; UPDATE = "Update"; DELETE = "Delete"

class AuthorizationResource(StrEnum):
    ORGANIZATION_ALGORITHM = "organization/{organization_id}/algorithm"
    ALGORITHM_SYSTEMCARD = "algorithm/{algorithm_id}/systemcard"

def permission(permissions: dict[str, list[str]]):
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            request = kwargs["request"]
            for resource, verbs in permissions.items():
                resource = resource.format_map(SafeDict(kwargs))      # sjabloon uit de route-kwargs
                granted = request.state.permissions                    # {"organization/42/algorithm": ["Read", ...]}
                if resource not in granted or any(v not in granted[resource] for v in verbs):
                    raise AMTPermissionDenied()
            return await func(*args, **kwargs)
        return wrapper
    return decorator

@router.get("/{organization_id}/algorithms")
@permission({AuthorizationResource.ORGANIZATION_ALGORITHM: [AuthorizationVerb.LIST]})
async def list_algorithms(request: Request, organization_id: int): ...
```

`request.state.permissions` wordt per verzoek opgebouwd uit regels `(role, resource-sjabloon, verbs)` in de database, ingevuld met de organisaties en algoritmes waar de gebruiker een rol heeft. ZAD neemt het sjabloon uit de route-kwargs over voor `Resource.path`, en niet de platte kaart met exacte vergelijking (5.2).
