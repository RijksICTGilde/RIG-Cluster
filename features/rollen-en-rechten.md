# Rollen en rechten

Eén plek zegt wie wat mag in ZAD, en die plek beslist voor de API en voor het portaal:
het regelregister in `operations-manager/python/opi/core/authz/rules.py`. De rolmatrix op
`/admin/rollen` is datzelfde register, als tabel. Wat daar staat is wat er wordt
afgedwongen; er is geen tweede lijst. Een cel die `v` toont kan in de praktijk nog dicht
zijn omdat de aanroep zijn doelwit niet noemt, nooit open: zie "Waartegen een scope weegt".

Ontwerp en besluiten: `plans/rechten-op-paden-tad-en-wies.md`.

## De rollen

Een lid van een project heeft een **lijst** rollen: `roles: [admin, ops]`. Rollen stapelen
(`ops` omvat `developer` omvat `viewer`, `admin` omvat `viewer`), en wie er meer dan een
heeft krijgt alles bij elkaar opgeteld.

| sleutel | label op het scherm | waarvoor |
|---|---|---|
| `viewer` | Viewer | lezen, behalve env-var-waarden en secrets |
| `developer` | Ontwikkelaar | bouwen op alles wat niet productie is |
| `ops` | Ops | hetzelfde op productie, en de OTAP-markering |
| `admin` | Admin | leden, rollen, tokens, service accounts, project verwijderen |
| `developer-ci` | Developer CI | voor een pijplijn: deployments die niet productie zijn |
| `ops-ci` | Ops CI | voor een pijplijn: image vervangen op elke deployment |

Admin is **gebruikersbeheer en geen bovenste trede**: een admin beheert wie erbij mag,
maar bouwt niet aan het project. Wie beide doet heeft `[admin, ops]`, en dat is wat elke
bestaande admin bij de migratie naar schemaversie 2.13 gekregen heeft.

Een platformbeheerder krijgt op elk project `[admin, ops]`, zonder lid te zijn.

## De matrix

`/admin/rollen`, voor platformbeheerders. Hij wordt uit het register gelezen, dus hij
staat hier niet nog een keer: elke regel krijgt een rij per scope die hij noemt, met een
kolom per rol. `v` mag, `v s` mag met een verse code uit je 2-factor, leeg mag niet.
Een service account kan geen 2-factor geven, dus alles met `s` is voor een pijplijn
dicht.

## Hoe je een regel toevoegt

1. Zet de action in `opi/core/authz/actions.py`. De naam is `<object>.<werkwoord>`,
   Engels, kebab-case.
2. Zet de regel in `opi/core/authz/rules.py`, met een label, een sectie en de grants:

   ```python
   rule(
       Action.DEPLOYMENT_DELETE,
       label="Deployment verwijderen",
       section=DEPLOYMENTS,
       grants=[
           Grant(Role.DEVELOPER, NON_PRODUCTION),
           Grant(Role.OPS, PRODUCTION, requires=STEP_UP),
       ],
   )
   ```

   Noem de LAAGSTE rol die het mag; de stapeling doet de rest. Een leesactie krijgt
   `read=True`. Lege grants mag: dan opent de action voor niemand, en de matrix toont die
   rij leeg.
3. Zet `@action(Action.X)` op de route, direct onder de routerdecorator en BOVEN de
   authenticatiedecorator.

De opstart faalt als een bewaakte route geen action draagt, als een action geen regel
heeft, als een rol in geen enkele grant voorkomt, of als een CI-rol alleen grants heeft
die een mens vragen. Een gat valt dus bij de start op en niet bij een aanroep.

Werkt een route niet op een project (een portaalpagina, platformbeheer), geef de regel dan
een `audience`: `Audience.SIGNED_IN` of `Audience.PLATFORM_ADMIN`. Zo'n regel heeft geen
grants; de sessiemiddleware respectievelijk `require_platform_admin` handhaaft hem.

## Hoe je een rol hernoemt op het scherm

`ROLE_LABELS` in `opi/core/authz/engine.py`. De **sleutel** (`developer`) staat in het
projectbestand, in de API en in de audit; de **weergave** (`Ontwikkelaar`) staat alleen
daar. Een andere naam op het scherm is dus een wijziging op een plek en geen migratie van
projectbestanden. Elke plek die een rol aan een mens toont leest dat label: de Team-kaart,
de wizard, de keuzelijst en de matrix.

`ROLE_DESCRIPTIONS` ernaast draagt de zin die bij de rol hoort, op dezelfde drie plekken.

## De spelregel voor een nieuwe rol

Een nieuwe rol komt er alleen als er een **echte houder** is die een strikte
deelverzameling nodig heeft, en de reden staat bij de rol in het regelbestand. Een rol die
alleen "net iets anders" is dan een bestaande maakt de matrix onleesbaar zonder dat iemand
er iets mee kan.

## Waar het wordt afgedwongen

- **API**: `opi/api/principal.py` bouwt de principal uit het bewijsmiddel en roept
  `decide()` aan.
- **Portaal**: `opi/web/authz.py`. `enforce_route_action` hangt als dependency op de
  web-router, leest de action van de route en beslist; een handler kan de poort dus niet
  vergeten. `authorize(...)` is dezelfde beslissing voor een handler die het project en
  het e-mailadres nodig heeft.
- **Sjablonen**: `can(action, deployment=..., component=...)`. Een knop toont zich als de
  ROLLEN volstaan; de step-up volgt bij het klikken. Er staat geen rolnaam meer in een
  sjabloon.
- **Step-up**: `@requires_step_up` zonder conditie leest de beslissing die de poort al
  op het verzoek zette (`request.state.authz_decision`), en daaruit of er een verse
  2-factor bij deze aanroep hoort. Zo dwingt de route af wat de matrixpagina toont.
  De mechaniek (dialoog, venster van vijf minuten, replay, `X-ZAD-OTP`) is onveranderd.

  Een eigen conditie blijft voor de twee gevallen waar geen projectbeslissing is om uit
  te lezen: de platformroutes (`platform.*`, een eigen `Audience` zonder grants) en de
  API-tokenroutes, die achter een SSO-bearer hangen in plaats van achter `handhaaf` en
  hun rolregel in `api_token_service` afdwingen. Daarnaast wat een scope niet kan zien:
  of een aanroep de OTAP-markering ZET of VERLAAGT staat in de body van de
  deployment-upsert, niet in het pad.

## Waartegen een scope weegt

Een scope leest de **resource**: de deployment waar de aanroep op werkt, haar
OTAP-markering, het component, en of het project ergens productie heeft. Die resource komt
uit één lezing van het projectbestand, in `build_resource` (`opi/services/project_roles.py`).

Het doelwit komt uit het **pad**: `deployment_name` en `component_name`. Noemt de aanroep
het niet, dan weegt hij tegen het **strengste in dit project**: de hoogste markering van
zijn deployments, en "in productie" voor een component. Zo kan een route die zijn doelwit
in de body of de query heeft nooit als niet-productie lezen, wat een developer-grant daar
zou openzetten. De uitkomst is dan strenger dan de matrix belooft, nooit ruimer.

Staat de deployment wel in het verzoek maar niet in het pad, dan zegt de route dat met
`@deployment_from(...)` naast `@action(...)`, met een functie die hem uit de argumenten van
de route haalt. De twee `:upsert-deployment`-routes doen dat (`deployment_in_body`); zonder
die verklaring zou een pijplijn met `developer-ci` niets meer kunnen zodra het project
ergens productie heeft.

`env-vars.manage` staat daarom op de componentscopes en niet op die van een deployment,
anders dan de rij in ontwerp paragraaf 4: de waarden hangen aan het component, en de route
die ze bewerkt heeft geen deployment in het pad. Een component dat in een
productiedeployment zit vraagt dus `ops`, ook op de deployment-variant van die route.

## Rollen schrijven

De Team-kaart en de API-route `PUT /api/projects/{project}/leden/{naam}/rol` (die
`roles: [...]` neemt) wijzigen één lid, en lopen langs `set_member_roles` in
`opi/services/api_token_service.py`. De team-wizard schrijft de hele ledenlijst in één
keer, dus hij gaat niet per lid door die functie; `bewaak_ledenlijst`
(`opi/forms/wizard/leden.py`) roept wel dezelfde guards aan als zij:

- `schoon_rollen`: weigert een lege lijst en een onbekende rol;
- `eis_een_admin`: weigert een lijst waarin niemand `admin` heeft.

Eén lezer per invariant, dus de drie wegen kunnen niet uit elkaar lopen op wat een
geldige rol is of op de eis dat er een admin overblijft. Het JSON-schema houdt daarnaast
zijn eigen `contains`-regel op `users`; dat is de laag die een bestand weigert dat buiten
deze wegen om is aangepast.

Opslaan vraagt `member.manage` met een verse step-up. Een wizard-flow die `users` niet
bezit en het toch indient wordt geweigerd.

## De OTAP-markering

`environment.set` hangt niet aan een route maar aan de schrijfweg: elke weg die de
markering van een deployment zet of verlaagt toetst hem, los van welk formulier of welke
route de gebruiker ervoor opende. In het portaal zijn dat de opslag van de modal-wizard
(`_process_and_save_modal_edit`) en de opslag van de volledige bewerkwizard
(`_save_existing_project`), op de API de twee `:upsert-deployment`-routes, die de
markering in hun body krijgen. Ops met step-up, want wie hem mag verlagen omzeilt elke
productieregel met één klik.

## Wat de body nog meer aanwijst

Een body kan een ANDER doelwit noemen dan het pad of `@deployment_from`. Dan toetst de
route die tweede handeling zelf, met `eis_handeling(request, project, action,
deployment=...)` (`opi/api/endpoint_util.py`), op de principal die de poort al herkende.
Op `:upsert-deployment` zijn dat er vijf:

| veld in de body | handeling | gewogen tegen |
|---|---|---|
| `deploymentName` | `deployment.write` | die deployment |
| `environment` | `environment.set` | het project |
| `cloneFrom` | `deployment.clone-from` | de BRONdeployment |
| `forceClone` (zonder `cloneFrom`) | `deployment.clone-from` | de bron die al in het projectbestand staat |
| `domain_format`, `subdomain`, `base_domain` | `deployment.domain-set` | het project |

`cloneFrom` kopieert de database van de bron naar het doel, dus de bron is wat geteld
moet worden. Klonen VANAF productie is daarom ops, ongeacht waar het naartoe gaat:
productiedata naar niet-productie kopieren laten we niet aan een developer of een
pijplijn over. Zonder die toets kon een `developer-ci` de productiedatabase naar een
ontwikkeldeployment klonen waarvan hij het image koos.

`forceClone` doet dat zonder een bron te noemen: de overslag op "mode once en afgerond"
wijkt ervoor (`database_manager.py`, en hetzelfde voor PVC's en buckets), dus de kloon
die al op de deployment staat loopt opnieuw. Dan weegt de bron uit het projectbestand.
Is die bron geen deployment (een backup, een externe bron), dan weegt de toets op
projectniveau en dus tegen de hoogste markering van het project.

`deployment.domain-set` geldt alleen voor een BESTAANDE deployment, en is dan altijd
ops. De domeingoedkeuring geldt per project (`is_deployment_domain_approved`) en het
format `subdomain` is `{subdomain}.{domain}`, dus twee deployments van hetzelfde project
kunnen dezelfde hostnaam vormen; wie de hostnaam van een draaiende deployment mag
verzetten kan die van productie opeisen. Een NIEUWE deployment krijgt zijn domeinvelden
mee uit `deployment.write`.

## Zie ook

- `features/leden-en-api-tokens.md`: leden, service accounts en tokens.
- `features/otap-markering.md`: de markering `environment` op een deployment.
- `features/otp-op-de-zad-gebruiker.md`: de 2-factor achter de step-up.
