# Een rollout wist de toestand van de vorige inhoud

## Wat het is

Als er nieuwe inhoud op een deployment wordt uitgerold -- een nieuwe image, of een upsert
van een bestaande deployment -- dan gaat alles wat diensten over de **vorige** inhoud
hadden vastgelegd op dat moment niet meer op. Een component dat uitstond omdat de oude
image OOM'de, een deployment die sliep omdat er niets gebeurde: de nieuwe inhoud is het
signaal dat die situatie voorbij is.

`HookPoint.REDEPLOY` is het moment waarop elke dienst zijn eigen toestand opruimt. Het is
de tegenhanger van [`HookPoint.DEPLOYMENT_STATE`](deployment-state-and-health.md): daar
vertelt een dienst wát hij deed, hier maakt hij het ongedaan.

## Waarom het er is

`update_image_and_regenerate` ruimde precies één soort toestand op, hardgecodeerd op de
reden:

```python
if is_disabled and is_image_pull_disable_reason(disabled_reason):
    ...  # weer aanzetten
```

Een component dat door de OOM-watcher was uitgezet, viel daarbuiten. Dat component bleef
op nul replicas staan: de image-update slaagde, de taak was groen, en er verscheen geen
deployment. Handmatig verversen hielp ook niet -- die weg gaat over `disabled-image`, en
dat veld schrijven alleen image-pull-uitschakelingen.

Daarnaast riep dezelfde functie sleep-mode bij naam aan om de slaapdeadline te verzetten.
Elke volgende dienst met toestand op een deployment zou daar weer een `if` bij vragen. Dat
is de vorm die voor de OOM-tuner al is opgeheven (die haakt op `AFTER_SYNC`), en die is nu
ook hier weg.

## De regels

**Het haakpunt heet naar de actie, niet naar de aanleiding.** Een image-update en een
upsert zijn voor vastgelegde toestand hetzelfde gebeuren. Een haak die "image vervangen"
had geheten, had de upsert als uitzondering erbij gekregen -- precies wat werd opgeruimd.

**Opruimen is onvoorwaardelijk.** Een dienst redeneert niet over de nieuwe inhoud. Wat de
opgeslagen reden ook zei, hij ging over inhoud die er niet meer is. Heeft de nieuwe inhoud
hetzelfde probleem, dan legt de waarnemende kant het opnieuw vast -- dan tegen de image
die het echt veroorzaakte.

**Opruimen is niet negeren.** Elke dienst geeft per opgeruimd ding één regel terug, in de
taal van de gebruiker. Die regels komen als `state_cleared` terug in het resultaat van de
image-update en de upsert, en staan in het log. Een component dat stil weer aangaat zonder
dat iemand kan zien waaróm hij uitstond, is erger dan een component dat uitblijft.

**Niet elke dienst wil dit.** Wie de haak niet beantwoordt, wordt niet gescand.

## Wie er nu op haakt

### deployment-health: zet het component weer aan

Elke automatische uitschakeling is een oordeel over de inhoud die er draaide:
`ImagePullBackOff` over een image die niet op te halen was, `OOMKilled` over een die het
geheugen opat, een crashloop over een die niet overeind bleef. Een rollout vervangt precies
die inhoud, dus het component gaat weer aan -- **ongeacht de reden**.

Het alternatief is echt overwogen: een OOM komt waarschijnlijk terug, dus alleen
image-pull opheffen scheelt één rondje aan-uit. Dat is de verkeerde afweging. Een nieuwe
image is vaak juist de fix voor het geheugenlek, niets anders heft een OOM-uitschakeling
ooit op, en een component dat voorgoed uitstaat zonder weg terug is erger dan een component
dat over vijf minuten weer uitgaat -- dan met een reden die naar de juiste image wijst. Het
kan ook niet uit zichzelf gaan flapperen: alleen een mens die iets uitrolt heft een
uitschakeling op.

Wat het níét doet: een `disabled` op de **componentdefinitie** (`components[]`) blijft
staan. Dat is een projectbrede keuze van een mens die over elke deployment gaat, en het
uitrollen van één deployment is niet het moment om die om te zetten.

### sleep-mode: wekt de deployment

De deadline gaat terug naar `now + sleep-after-deploy`, zodat een preview waar actief aan
gewerkt wordt niet tussen twee pushes in slaap valt.

Een **slapende** deployment krijgt niet alleen een latere deadline, maar wordt gewekt.
Nieuwe inhoud die op nul replicas blijft staan, is niet uitgerold: er start geen pod, dus
niets pakt hem op, en degene die pushte ziet een geslaagde taak met een deployment die nog
het oude draait. Dat kost een koude start op een deployment die net is aangeraakt -- het
moment waarop iemand daar het minst last van heeft.

Staat sleep-mode uit voor dit cluster/project, of valt de deployment buiten de
`match`-selectie, dan gebeurt er niets.

## Zelf aanhaken

```python
def on_redeploy(self, ctx: RedeployContext) -> list[str]:
    if not <ik heb hier iets vastgelegd>:
        return []
    <ruim het op in ctx.project_data>
    return ["Wat er is opgeruimd, en waarom."]
```

`ctx` geeft `project_name`, `project_data` (in-memory, muteer in place), `deployment`,
`deployment_name`, `cluster` en `component_names` -- de componenten waar deze rollout
nieuwe inhoud op zet. Is die lijst leeg, dan raakte de actie geen component in het
bijzonder; een per-component toestand heeft dan niets op te ruimen, een toestand van de
hele deployment wel.

**Nooit zelf committen.** De aanroeper commit de rollout en alle opruimingen in één keer,
zodat twee diensten niet om twee commits kunnen racen.

## Waar het vandaan komt

- Haakpunt en context: `opi/services/services_enums.py` (`HookPoint.REDEPLOY`),
  `opi/services/catalog/base.py` (`RedeployContext`, `Service.on_redeploy`)
- De scan: `opi/services/redeploy.py` (`run_redeploy_hooks`)
- Bewoners: `opi/services/catalog/deployment_health/__init__.py`,
  `opi/services/catalog/sleep_mode/__init__.py`
- Aanroepers: `opi/manager/project_manager.py` --
  `update_image_and_regenerate` en `_upsert_deployment_once` (alleen de update-tak; een
  nieuwe deployment heeft geen eerdere toestand)
- Tests: `tests/test_redeploy_hook.py` (wat de diensten doen),
  `tests/test_redeploy_on_rollout_paths.py` (dat de paden de haak ook echt aanroepen)
