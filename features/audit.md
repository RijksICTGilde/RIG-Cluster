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
| `actor` | het e-mailadres of de naam uit `Actor` (`opi/core/actor.py`), leeg waar de weg geen identiteit draagt |
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

Er is nog geen route en geen API die de tabel leest of schrijft: de hook die
`record_action` bij elke rechtenbeslissing aanroept en het audit-tabblad op de
projectpagina komen in taak 3 (`plans/rechten-3-audit-koppeling.md`).

## Bewaartermijn

Twee klokken op dezelfde rij, uit punt 6 van het gebeurtenissendocument:

| na | wat er gebeurt |
|---|---|
| 90 dagen | `actor` en `origin` gaan op `NULL` |
| een jaar | de rij gaat weg |

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

## Zie ook

- `plans/rechten-op-paden-tad-en-wies.md` paragraaf 6 en 8.2: het ontwerp en de vaste
  signatuur van `record_action`
- `features/futures/gebeurtenissen-vastleggen-en-melden.md` punt 6 en 7: het datamodel en
  de bewaartermijn
