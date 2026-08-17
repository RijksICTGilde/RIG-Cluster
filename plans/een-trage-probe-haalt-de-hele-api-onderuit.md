# Een trage probe haalt de hele API onderuit

Onder belasting valt de API even volledig weg met een `503` van nginx. Niet omdat OPI omvalt, maar omdat één readiness-probe zijn timeout niet haalt en de pod daarmee meteen uit de service verdwijnt.

Gevonden in de reallife-run (RC-112), `docs/reallife-run-2026-08-14-rc112.md`.

## Niet urgent, en dat is gemeten

`readinessProbe.failureThreshold: 1` staat in `bootstrap/rig-system/kustomize/operations-manager/base/deployment.yaml:62` **sinds februari 2026** ("Local development", #24). Dit is dus geen regressie van deze release; het gedrag was er al en is nu voor het eerst zichtbaar gemaakt.

De omstandigheid waarin het optrad is bovendien zwaarder dan productie: de reallife-suite én de punt-14-jacht tegelijk op één sandbox van vier cores, samen zo'n tien gelijktijdige ArgoCD-wachten plus schrijvers op het projectbestand.

**Deze taak hoeft de release dus niet tegen te houden.** Hij hoort er wel te komen, want het is een echte beschikbaarheidsfout en een client kan hem niet onderscheiden van een echte storing.

## Wat er precies gebeurt

```
POST /api/v2/projects/p1492-4f2/:upsert-deployment?rollout=false -> 503
<center><h1>503 Service Temporarily Unavailable</h1></center>
<hr><center>nginx</center>
```

De pod was niet omgevallen (`Restart Count: 0`), maar:

```
Warning  Unhealthy  Readiness probe failed: Get "http://10.244.0.53:8000/readyz":
                    context deadline exceeded (Client.Timeout exceeded while awaiting headers)
```

`/readyz` leest een vlag uit het geheugen en doet zelf niets zwaars. "Deadline exceeded" betekent hier dus dat de handler **niet aan de beurt kwam**: de eventloop stond langer dan de vijf seconden probetimeout vol. In diezelfde periode duurden store-persists tot 9,2 seconden.

De huidige instelling:

```yaml
readinessProbe:
  httpGet: { path: /readyz, port: 8000 }
  periodSeconds: 10
  timeoutSeconds: 5
  failureThreshold: 1
```

Met `failureThreshold: 1` haalt één trage meting de pod uit de endpoints van de service. Draait er maar één replica, dan heeft de service daarna geen enkele endpoint en beantwoordt nginx **elke** lopende aanroep met een 503 -- ook aanroepen die niets met de drukte te maken hebben.

## Taken

### 1. De probe mag niet op één meting afgaan

`failureThreshold` omhoog (3 is de Kubernetes-standaard en past bij de liveness-probe ernaast, die al op 3 staat). Daarmee moet de pod drie metingen achter elkaar missen voor hij uit de service gaat, en dat is een echte storing in plaats van een drukke seconde.

Overweeg tegelijk `timeoutSeconds`. Vijf seconden is ruim voor een handler die een vlag uit het geheugen leest; het probleem is niet de handler maar de wachtrij ervoor. Laat dit staan tenzij je kunt onderbouwen waarom het anders moet.

**Let op wat je niet kapot maakt.** Een readiness-probe die te traag reageert houdt verkeer naar een pod die het echt niet meer doet. Schrijf op waarom de gekozen waarde die afweging goed maakt: hoe lang duurt het nu voordat een werkelijk kapotte pod uit de service gaat, en is dat acceptabel.

Verifieer: dezelfde dubbele belasting nabouwen (de reallife-suite en de punt-14-jacht tegelijk) en zien dat er geen 503 meer komt. Zonder die reproductie is de reparatie niet aangetoond.

### 2. Uitzoeken wat de eventloop vasthoudt

Dit is het eigenlijke probleem; taak 1 dempt alleen het gevolg.

Store-persists van 9,2 seconden betekenen dat er synchroon werk in de async-lus zit. Kandidaten om te meten, niet om aan te nemen:

- git-aanroepen in het opslaanpad (`subprocess`, per aanroep een fork);
- SOPS/AGE-versleuteling van projectbestanden;
- `kubectl`-aanroepen die niet in een thread staan.

Meet het met de eventloop-debugmodus van asyncio (`loop.set_debug(True)` logt taken die te lang blokkeren) of met een profiel op het moment van de piek. Lever een lijst met de langste blokkades en hun herkomst.

Repareren hoort in een eigen taak, met een eigen meting ervoor en erna. Deze taak levert het bewijs.

### 3. Meer dan één replica?

De 503 was er alleen omdat er één replica draait: met twee had de service nog een endpoint gehad. Onderzoek of dat kan, of dat er iets in OPI zit dat maar één keer mag draaien (de takenclaim en de scheduler zijn de plekken om te controleren).

Dit is geen opdracht om het aan te zetten. Wel om het antwoord op te schrijven, want zolang we één replica draaien is elke herstart van die pod een korte onderbreking van alles.

## Wat er buiten valt

- De liveness-probe; die staat al op 3 en heeft dit gedrag niet.
- Het opschalen zelf, als taak 3 uitwijst dat het kan. Dat is dan een eigen beslissing.
