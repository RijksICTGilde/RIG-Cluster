# Diensten die elkaars toestand kennen, en een gezondheidscheck die dat gebruikt

## Wat het is

Een deployment kan in een situatie zitten die een dienst zelf heeft veroorzaakt. De
duidelijkste is de slaapstand: sleep-mode schaalt de componenten naar nul en zet er een
wekker voor in de plaats. Tot RC-28 wist niets buiten die dienst dat, en moest generieke
code de situatie afleiden uit wat het cluster liet zien.

Dat ging mis. Een herverwerking meldde "productie - frontend: image ophalen mislukt ...
zad-waker:latest" voor een component dat niet draaide en dat image niet gebruikt, terwijl
ArgoCD de applicatie Synced en Healthy noemde. De wekker draagt bewust hetzelfde
`app`-label (hij moet de Service van het component overnemen), dus de check las diens
toestand als die van het component. Achter die melding hangt logica die een component
uitschakelt bij een image-pull-fout.

Dit onderdeel bestaat uit drie dingen:

1. een haakpunt waarop een dienst **toestand** bijdraagt aan een deployment;
2. de gezondheidscheck als **systeemdienst**, die die toestand meeweegt voor hij oordeelt;
3. dezelfde toestand **getoond** op de deploymentweergave.

## Feiten, geen oordelen

Het dragende onderscheid: een dienst levert een **feit** ("deze deployment slaapt, dus
nul applicatiepods is de bedoeling"), nooit een gezondheidsoordeel. Als "ik slaap" te
formuleren was als "en dus is alles in orde", dan maakt een dienst met een verkeerde
toestand een echte storing onzichtbaar.

Daarom is het oordeel asymmetrisch:

| Waarneming | Weegt de toestand mee? |
|---|---|
| Een probleem op een draaiende applicatiepod (OOM, CrashLoopBackOff, image ophalen mislukt) | **Nee.** Altijd een fout. De toestand wordt meegegeven en krijgt expliciet geen stem. |
| Géén applicatiepods | **Ja.** Alleen de dienst die naar nul schaalde weet waarom het stil is. |

`DeploymentStateFact` heeft dus geen oordeelveld, en `tests/test_deployment_state.py`
faalt zodra iemand er een toevoegt.

## Hoe je het gebruikt

### Een dienst laat weten wat hij deed

Implementeer `Service.deployment_state(ctx)` en geef nul of meer feiten terug. Het
antwoord komt uit het **projectbestand** (waar een dienst zijn eigen toestand bijhoudt),
niet uit het cluster, dus de hook is synchroon en gebruikt geen connectors.

```python
def deployment_state(self, ctx: DeploymentStateContext) -> list[DeploymentStateFact]:
    sleep = read(ctx.project_data, ctx.deployment_name)
    if sleep.state == STATE_SLEEPING:
        return [
            DeploymentStateFact(
                service=self.service_type.value,
                summary="Deze deployment slaapt: de componenten zijn naar nul geschaald ...",
                expects_no_application_pods=True,
            )
        ]
    return []
```

`expects_no_application_pods` is de enige operationele consequentie die een dienst mag
uitspreken. Hij zegt dat de eigen pods van de applicatie horen te ontbreken -- niets over
pods die er wél zijn.

### De badge: het woord op de kaart (RC-35)

Naast `summary` draagt een feit een optionele `badge`: het ene of twee woorden waarmee de
deploymentkaart de situatie benoemt. Tekst, verder niets -- geen kleur, geen icoon, want een
kaart vol door diensten gekozen opmaak leest niet meer als één platform.

Samen met `expects_no_application_pods` bepaalt de badge waar het woord terechtkomt:

| Feit | Waar het woord staat |
|---|---|
| badge + `expects_no_application_pods=True` | **In plaats van** de groene `Healthy` die nul replicas oplevert |
| badge, `expects_no_application_pods=False` | **Naast** de gezondheidsbadge |
| geen badge | Niet op de kaart; alleen de `summary` in het toestandsblok |

Alleen de groene `Healthy` wordt ooit vervangen. `Degraded`, `Progressing` en `Unknown`
zijn iets dat echt is waargenomen, en een toestand die die verbergt zou van iets
uitschakelen een manier maken om een storing te laten verdwijnen.

Melden twee diensten tegelijk een vervangende badge -- een deployment die slaapt én
helemaal uit staat -- dan krijgen ze **allebei** hun woord, gesorteerd op dienstnaam zodat
de weergave niet van de volgorde van de registry afhangt. Eén gedeelde badge "niet actief"
zou verbergen of er iets van de gebruiker verwacht wordt: slapen gaat vanzelf over,
uitgeschakeld blijft tot iemand het aanzet.

Wat waar staat, ligt daarmee vast: de **badge** benoemt de situatie, het **blok** eronder
draagt de zin die niet op een badge past (waarom het zo is, en wat het beëindigt). Geen van
beide is een verkorte versie van de ander.

```python
state = collect_deployment_state(project_data, deployment_name)
state.replacing_badges     # woorden die de plaats van Healthy innemen
state.accompanying_badges  # woorden die ernaast staan
```

### Iets vraagt de toestand op

```python
from opi.services.deployment_state import collect_deployment_state

state = collect_deployment_state(project_data, deployment_name)
state.facts                        # wat de diensten melden
state.expects_no_application_pods  # zegt een dienst dat nul pods de bedoeling is?
```

De collector scant `HookPoint.DEPLOYMENT_STATE` en noemt geen enkele dienst bij naam. Hij
filtert bewust **niet** op `applies_to`: sleep-mode kan clusterbreed aanstaan zonder dat
een project hem kiest, dus een selectiefilter zou de toestand wegfilteren van precies de
deployments die slapen. Een dienst die niets deed meldt niets.

## De gezondheidscheck als systeemdienst

`deployment-health` is een `ServiceKind.SYSTEM`-dienst: altijd aan, nooit in de
services-lijst, niet kiesbaar. De dienst bezit het **oordeel**; het waarnemen (kubectl,
plannen, remediatie) blijft in `opi/services/oom_watcher.py`. Dezelfde splitsing die
resource-tuning al had.

| Methode | Vraag |
|---|---|
| `counts_as_failure(health, state)` | Is deze waarneming een fout van de applicatie? (altijd ja bij een probleem; de toestand krijgt geen stem) |
| `absent_pods_are_expected(state)` | Waarom heeft dit component geen pods? Een zin van de dienst die naar nul schaalde, of `None` |

Wat dat in de praktijk verandert:

- een slapende deployment zonder pods meldt tijdens het uitrollen "Deze deployment
  slaapt ..." in plaats van "pods worden aangemaakt";
- een niet-slapende deployment zonder pods meldt dat onverkort;
- `describe_components_waiting` koppelt pods niet langer op het kale `app`-label. De
  wekker draagt dat label bewust, dus die functie las zijn `ImagePullBackOff` als de reden
  van het component -- letterlijk de melding uit het incident, en het deel dat de labelfix
  in `4b86aed7` niet raakte.

## Op de deploymentweergave

`project-details/section-deployment-state.html.j2` toont per deployment de gemelde
feiten. Het is dezelfde bijdrage, alleen gerenderd in plaats van beoordeeld: de template
loopt over wat er gemeld wordt en noemt geen dienst, dus een volgende dienst die een
deployment in een situatie zet krijgt het blok gratis. Meldt niemand iets, dan is er geen
blok.

De deploymentkaart (`project-details/_argocd-deployment-card.html.j2`) leest sinds RC-35
uit dezelfde feiten: de badge, en of er dingen weggelaten moeten worden die alleen zin
hebben zolang er iets draait (de logs-knop). De kaart kent dus geen enkele dienst bij
naam; hij krijgt `deployment_states` (naam → `DeploymentState`) mee en leidt de rest af.

## Configuratie

Geen. `deployment-health` neemt geen configuratie aan en `deployment_state` is een
gedragshook, geen instelling.

## Afhankelijkheden

- `HookPoint.DEPLOYMENT_STATE` in `opi/services/services_enums.py`
- `DeploymentStateContext` / `DeploymentStateFact` in `opi/services/catalog/base.py`
- `opi/services/deployment_state.py` (collector)
- `opi/services/catalog/deployment_health/` (het oordeel)
- `opi/services/oom_watcher.py` (het waarnemen)
- `SERVICE_ROLE_LABEL_KEY` / `application_pod_selector` -- de voorwaarde onder dit alles:
  een applicatiebrede opzoeking slaat alles over dat `zad-role` draagt

## Zie ook

- `features/oom-kill-watcher.md` - de waarnemende kant
- `features/sleep-mode.md` - de eerste bewoner van het haakpunt
- `features/uitgeschakeld-is-niet-gezond.md` - de tweede: `deployment-health` meldt zelf
  dat een deployment uit staat, en voedt daarmee de kaart, de banner en de V2 API
- `features/redeploy-clears-recorded-state.md` - de schrijvende tegenhanger: wat een dienst
  hier meldt, ruimt hij daar op zodra er nieuwe inhoud wordt uitgerold
- `instructions/services.md` - alle haken die een dienst kan implementeren
