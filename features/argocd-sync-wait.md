# Wachten op ArgoCD, en wat daarbij een fout is

Na het uitrollen wacht OPI tot de ArgoCD-applicaties van het project gesynct en gezond zijn.
Die wachtstap meldt nu wát hem tegenhield, kijkt alleen naar de deployments die de taak zelf
heeft aangeraakt, en noemt een applicatie die zelf niet start geen fout van het uitrollen.

## Het probleem

De wachtstap eiste dat **alle** applicaties van het project binnen ongeveer 43 seconden
`Synced` waren en `Healthy` of `Progressing`. Lukte dat niet, dan werd de substap als mislukt
gemarkeerd met deze tekst:

> De wachttijd op ArgoCD is verstreken: niet alle apps van '<project>' waren binnen de
> wachttijd gesynct en gezond. Controleer de status van het project.

Daar zaten drie problemen in.

**Eén kapotte omgeving blokkeerde alle andere.** Een project als `wies` heeft naast `main` een
stuk of acht PR-omgevingen. Zodra één daarvan `Degraded` is, bijvoorbeeld door een image dat
niet gepulld kan worden, wordt die nooit meer gezond. Vanaf dat moment liep élke deploy van
élk ander deployment in dat project gegarandeerd in de time-out, hoe goed die deploy zelf ook
ging. Pas als iemand de kapotte PR-omgeving opruimde, werd het weer groen.

**Het werd als fout van ons gerapporteerd.** Een applicatie waarvan de pods crashen is een
probleem van die applicatie, niet van het uitrollen. De manifests waren gegenereerd, gecommit,
gepusht en door ArgoCD opgepakt. Elders in de code staat dat onderscheid al expliciet
(*"app crashing, not a deploy failure"*), hier niet. Erger nog: de melding kwam als `ERROR` in
het log, en de log-watcher (`opi/services/log_watcher.py`) maakt van nieuwe ERROR-regels een
ntfy-notificatie. Elke valse rode substap werd dus een pushbericht.

**Je kon niet zien waaróm.** De per-app status werd op DEBUG gelogd en de melding noemde geen
enkele applicatie en geen enkele status. In productie draait het log op INFO, dus de vraag
"waarom kon ArgoCD niet syncen" was niet te beantwoorden zonder debug-logging aan te zetten.

De melding sprak bovendien de wizard tegen, die de gebruiker zelf vertelt: *"Een eventuele
time-out-melding betekent niet dat het aanmaken is mislukt."*

## Hoe het nu werkt

| Situatie | Wat er gebeurt |
|---|---|
| Alle betrokken apps `Synced` + `Healthy`/`Progressing` | Substap voltooid, geen meldingen. |
| Een app is `Degraded` of `Missing` | Substap **voltooid**, plus een melding die de app en zijn status noemt en uitlegt dat het aan de applicatie ligt. Log op WARNING. |
| Apps nog `OutOfSync` of `Progressing` bij het verstrijken van de wachttijd | Substap **voltooid**, plus een melding met de status per app en de mededeling dat ArgoCD het zelf afrondt. Log op WARNING. |
| ArgoCD niet bereikbaar | Onveranderd: dit blijft een echte fout, met `fail_task` en een ERROR. |

Alleen het laatste geval is een fout van ons, en alleen dat geval bereikt de log-watcher.

## Scope: alleen wat de taak zelf aanraakte

`_monitor_argocd_and_deployment` accepteert `deployment_names`. De taak-handler geeft door wat
hij verwerkte (`deployment_name` of `deployment_names` uit de payload), en de wacht kijkt dan
alleen naar `<project>-<deployment>`. Zonder die parameter blijft het oude, projectbrede gedrag
gelden, want niet elke aanroeper heeft een scope.

Dit is de eigenlijke oplossing voor het eerste probleem: een kapotte PR-omgeving van iemand
anders komt niet meer voor in jouw wachtstap.

## Wat de melding nu bevat

```
wies-pr-478 (sync=Synced, health=Degraded)
```

Per blokkerende applicatie de naam, de sync-status en de health-status, zowel in de melding die
de gebruiker ziet als in de logregel. Daarmee is "waarom wachtte hij" direct te beantwoorden.

## Wat bewust niet is aangepast

De wachttijd zelf blijft ongeveer 43 seconden (5 s + 8 s + 15 pogingen van 2 s). Nu een
niet-afgeronde sync geen fout meer is, is de lengte van dat venster minder belangrijk: je krijgt
een informatieve melding in plaats van een rode stap. Of de wachttijd omhoog moet, is een aparte
afweging die je pas goed kunt maken met de nieuwe logging erbij.
