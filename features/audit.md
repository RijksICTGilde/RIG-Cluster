# Audit: wie deed wat

## Wat het is

Een doorzoekbare lijst van handelingen op het platform: elke toegestane schrijfactie en
elke weigering, met de actor, het object, het besluit en het tijdstip erbij (BIO2 8.15.01
vraagt per logregel actie, object, resultaat, oorsprong, actor en tijdstempel).

Daarvoor stond "wie deed wat" verspreid: als trailer op een commit van een projectbestand
en in `async_tasks.created_by`. Er was geen lijst om in te zoeken, en een weigering werd
nergens vastgelegd.

De rijen staan in de gebeurtenissentabel `notification_events`, waar de audit de eerste
schrijver van is. Het datamodel komt uit
`features/futures/gebeurtenissen-vastleggen-en-melden.md` punt 7; de rest van dat plan
(postvak, ontvangers, push-kanalen, export) is nog niet gebouwd.

## Wat er wordt vastgelegd

Eén rij per beslissing, met twee soorten in de kolom `type`:

| type | wanneer |
|---|---|
| `nl.rig.zad.action.performed.v1` | de actie mocht en is uitgevoerd |
| `nl.rig.zad.action.denied.v1` | de actie is geweigerd |

De rij draagt:

| kolom | inhoud |
|---|---|
| `subject` | `<project>` of `<project>/<deployment>`, nooit een e-mailadres |
| `project`, `deployment` | het object, apart doorzoekbaar |
| `actor` | het e-mailadres of de naam uit `Actor` (`opi/core/actor.py`); altijd gevuld, ook voor een service account of een scheduler |
| `actor_kind` | de soort uit `ActorKind`: `user`, `project-api-key`, `service-account`, `platform-api-key`, `scheduler` |
| `origin` | het client-IP zoals de aanroeper het meegeeft |
| `summary` | een korte Nederlandse regel, zoals `demo/live: deployment.delete door ops` |
| `severity` | `informational` bij een toegestane actie, `actionable` bij een weigering |
| `category` | `beheer-en-beveiliging` |
| `flow_id`, `task_id` | de correlatie met de logregels en met de taak, als er een taak startte |
| `data` | de action, het resourcepad, de markering, de grant, de cause, de step-up, de statuscode, de commit-SHA's en de velden die de route expliciet noemt |

`summary` en `subject` dragen nooit een `@`: een persoonsgegeven hoort niet in de
context-attributen van een CloudEvents-vormig record. De rol uit de grant komt wel in
`summary` (de grant zelf is `ops@production`, de rol is `ops`).

`source` draagt de vorm `urn:nld:oin:<OIN>:systeem:zad-<cluster>`. Er is nog geen OIN
toegekend, dus `EVENT_SOURCE_OIN` staat op de placeholder `oin-nog-niet-toegekend`.
Zolang die erin staat is een rij CloudEvents-VORMIG en geen geldig CloudEvent; een
exporteur hoort hem te weigeren.

## Hoe de rij ontstaat

Er is geen code per route. De poort van het rechtensysteem zet bij ELKE beslissing de
action, de resource, de uitkomst en de step-up op het verzoek (`record_decision` in
`opi/core/authz/engine.py`), en een middleware schrijft daar na het antwoord de rijen uit:
`AuditMiddleware` in `opi/middleware/audit.py`.

Eén rij per BESLISSING, niet per verzoek: een upsert met `cloneFrom` weegt eerst het doel
(`deployment.write`) en daarna apart de bron (`deployment.clone-from`), en beide staan in
de audit. Een toegestane leesactie levert geen rij, een geweigerde leesactie wel.

De rij wordt na het versturen van het antwoord geschreven, als achtergrondtaak met een
eigen databasesessie, dus de audit houdt geen antwoord op.

Drie dingen zijn alleen tijdens het verzoek bekend en reizen als contextvar mee
(`opi/core/audit_trail.py`, zoals `opi/core/actor.py` dat voor de actor doet). De hook
OPENT dat spoor aan het begin van het verzoek; buiten een verzoek legt een schrijver
niets vast, zodat de nachtelijke tuner geen SHA's opstapelt in een context die niemand
leest:

| wat | wie legt het vast |
|---|---|
| de commit-SHA's van het projectbestand | de projectstore, op de enige plek waar een commit ontstaat (`_commit_and_publish`); een idempotente opslag levert er geen |
| de taak-id | `async_task_service.create_task`, als het verzoek een taak startte |
| de velden die de route noemt | de poort, uit het pad, de querystring of het formulier |

Daarom staat de middleware als BINNENSTE om de router: een `BaseHTTPMiddleware` draait de
rest van de applicatie in een eigen taak, en een contextvar die daarbinnen wordt gezet
komt dan niet meer terug.

### De velden die een route noemt

Waar de waarde niet in het projectbestand landt (welke snapshot, welk image) noemt de
route zijn velden zelf met `@action(..., audit_fields=(...))`, en alleen die, nooit een
hele body. Vier routes doen dat; `tests/test_audit_hook.py` legt de lijst vast, zodat een
nieuwe route er bewust bij komt.

`velden=` van `@requires_step_up` staat daar LOS van: dat is wat de step-up-dialoog moet
herhalen, en daar hoort ook een vrij tekstveld bij (de env-vars van een component, een
rol, een lidnaam). Die twee lijsten stonden gekoppeld; daardoor liftte de env-var-route
mee en zou de hele platte env-var-tekst, met wachtwoorden en adressen, een jaar in
`data.fields` blijven staan.

De poort leest die velden pas NADAT ze de aanroeper heeft toegelaten, en kapt elke
waarde af op 256 tekens (`MAX_VELDWAARDE`). Twee redenen, beide gemeten op
`POST /projects/{project}/jobs`, dat zijn velden uit het formulier haalt: een geweigerde
(ook anonieme) aanroep haalde anders de hele urlencoded body door het geheugen voordat de
403 viel, en `data` is JSONB zonder kolombreedte, dus zo'n body belandde ongeknipt in een
rij die een jaar blijft staan (400 kB per verzoek, zonder bovengrens).

**Er mag nooit een persoonsgegeven in `fields`.** De bewaarlus zet na 90 dagen alleen
`actor` en `origin` op NULL; wat in `data.fields` staat blijft een jaar staan. Een route
waarvan een veld een e-mailadres, een lidnaam of een IP-adres kan dragen noemt dat veld
dus niet: de commit-SHA wijst de wijziging dan aan.
`tests/test_audit_hook.py` loopt alle routes af en faalt op zo'n veld.

`actor` is altijd gevuld, ook voor een scheduler (een procesnaam is geen
persoonsgegeven) en voor een service account. Het gebeurtenissenontwerp liet die kolom
leeg voor de projectsleutel, omdat die geen persoon droeg; sinds schemaversie 2.12 heeft
elk project tokens per rol en kent elke ingang zijn actor.

## Wat je ziet

Op de projectpagina een tabblad **Audit** (`/projects/<naam>/audit`), achter
`Action.AUDIT_READ`: tijd, wie, wat, waarop, uitkomst, en een link naar de commit als die
er is. Het register geeft die action aan `admin`; een platformbeheerder heeft hem op elk
project, en wie hem niet heeft ziet het tabblad niet en krijgt op de route een 403.

Het project komt uit het PAD, dezelfde naam waarover `decide()` besliste; `list_actions`
controleert zelf niets, dus een naam uit de querystring of de body zou de poort en de
lezing over twee verschillende projecten laten gaan. `?limit=` bepaalt het aantal rijen,
met een standaard van 50 en een plafond van 200 (een grotere waarde geeft 200 rijen en
geen fout). Bladeren gaat met een `before`-tijdstip, niet met een offset.

`origin` staat niet op de pagina: die kolom is voor platformbeheerders, en die lezen hem
in de database.

## Hoe je het gebruikt

`opi/services/audit.py` is de enige schrijver en de enige lezer:

```python
await record_action(
    actor=get_actor(),
    action="deployment.delete",
    resource_path="project/demo/deployment/live",
    project="demo",
    deployment="live",
    environment="productie",
    allowed=True,
    cause=None,
    grant="ops@production",
    step_up=True,
    status_code=200,
    commit_shas=["abc1234"],
    task_id=None,
    origin="10.0.0.7",
)

rijen = await list_actions("demo", limit=50)              # nieuwste eerst
ouder = await list_actions("demo", limit=50, before=rijen[-1].time)
```

`record_action` gooit nooit naar zijn aanroeper: een kapotte audit mag geen uitrol
tegenhouden. Een mislukte schrijfactie gaat met `logger.exception` naar de log en telt op
in de Prometheus-teller `opi_audit_write_failures_total`, zodat het niet stil gebeurt.

## Bewaartermijn

Twee klokken op dezelfde rij, uit punt 6 van het gebeurtenissendocument:

| na | wat er gebeurt |
|---|---|
| 90 dagen | `actor` en `origin` gaan op `NULL` |
| een jaar | de rij gaat weg |

De lus raakt alleen de auditsoorten (`ACTION_TYPES`). De audit is vandaag de enige
schrijver van de tabel, maar het gebeurtenissenontwerp geeft een latere soort zijn eigen
termijn (een beveiligingsgebeurtenis houdt de actor langer), en zonder die grens zou zo'n
soort stil met de termijn van de audit worden gewist.

Negentig dagen voor het persoonsgegeven, dezelfde termijn als de Keycloak-auditevents.
Wat er daarna overblijft is "wat is er met dit project gebeurd", en dat is een
beheergeschiedenis; wat verdwijnt is "wie deed het en waarvandaan".

De lus zet die twee kolommen op `NULL` en niet op een tekst zoals `verwijderd`. Dat is
geen smaak: de index `idx_notification_events_actor_sweep` is partieel op
`actor IS NOT NULL OR origin IS NOT NULL`, dus een rij met een tekst in `actor` valt daar
nooit meer uit en die index zou monotoon groeien. `actor_kind` blijft wel staan, want
"mens" of "scheduler" wijst niemand aan; daaraan ziet een scherm dat er een persoon is
weggehaald in plaats van dat er nooit een was.

## Configuratie

| instelling | standaard | wat het doet |
|---|---|---|
| `EVENT_SOURCE_OIN` | `oin-nog-niet-toegekend` | de OIN in `source`; de standaard is een placeholder |
| `AUDIT_RETENTION_HOUR` | `4` | het uur (Europe/Amsterdam) waarop de bewaarlus draait |

Er is geen schakelaar om de bewaarlus uit te zetten: zonder lus bewaart de tabel
persoonsgegevens zonder termijn. Hij start met de taakwerker mee
(`opi/core/audit_retention_scheduler.py`) en draait één keer per etmaal.

## Afhankelijkheden

- `notification_events`, Alembic-migratie `013`, model in
  `opi/services/persistence/notification_events.py`
- `Actor` en `ActorKind` uit `opi/core/actor.py`
- de teller in `opi/core/metrics.py`
- het rechtensysteem: `record_decision` en het regelregister (`opi/core/authz/`)
- `client_ip` uit `opi/utils/client_ip.py` voor `origin`

## Zie ook

- `plans/rechten-op-paden-tad-en-wies.md` paragraaf 6 en 8.2: het ontwerp en de vaste
  signatuur van `record_action`
- `features/futures/gebeurtenissen-vastleggen-en-melden.md` punt 6 en 7: het datamodel en
  de bewaartermijn
