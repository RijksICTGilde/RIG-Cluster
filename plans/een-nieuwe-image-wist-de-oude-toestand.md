# Een nieuwe image wist de oude toestand

Status: plan, 6 augustus 2026. Niet gebouwd. Aanleiding: bij een image-update wordt maar één soort toestand opgeruimd, hardgecodeerd, en de rest blijft staan. Dat is ook de beste kandidaat voor de melding van Maarten-Jan dat een image-update wel een taak oplevert maar geen deployment.

## Wat er nu gebeurt

`update_image_and_regenerate` in `project_manager.py` doet dit:

```python
# Re-enable component if it was disabled due to an image pull error
if is_disabled and is_image_pull_disable_reason(disabled_reason):
    ...
```

Dus: een component dat is uitgezet omdat de image niet op te halen was, komt terug. Een component dat om een **andere** reden is uitgezet, door de OOM-watcher of via `resource_router`, blijft op nul replicas staan. De taak slaagt, er verschijnt geen deployment, en handmatig verversen werkt wel omdat dat pad `force_reenable` gebruikt en niet naar de reden kijkt.

Dat past precies op de melding, en het is met één gegeven te toetsen: wat staat er bij `disabled-reason` op dat component.

Met de slaaptoestand gebeurt helemaal niets. Een slapende deployment die een nieuwe image krijgt, blijft slapen, en de slaapdeadline wordt niet verzet terwijl er net werk is verzet.

## Waarom dit een haak hoort te zijn

De regel hierboven staat in `project_manager.py` en noemt één specifieke reden bij naam. Elke dienst die in de toekomst een toestand op een deployment zet, moet daar dan opnieuw een `if` bij. Dat is de vorm die we voor de OOM-tuner al hebben opgeheven: die is een systeemdienst geworden die op `AFTER_SYNC` haakt, en de generieke code scant de registry in plaats van de dienst bij naam te kennen.

Er zijn nu twee haakpunten met drie bewoners:

```
after-sync         Resource tuning
deployment-state   Slaapstand, Deployment gezondheid
```

Wat ontbreekt is een moment "de image van dit component is vervangen", waarop een dienst zijn eigen toestand mag opruimen. Sleep-mode zet de deadline opnieuw, deployment-gezondheid heft zijn uitschakeling op, en een volgende dienst doet wat bij hem past, zonder dat `project_manager` weet wie er luistert.

## Voorstel

1. **Een haakpunt voor "de image is vervangen"**, met het component en de nieuwe image erbij. Zelfde vorm als de bestaande twee, zodat er niets nieuws te leren valt.
2. **Deployment-gezondheid ruimt zijn uitschakeling op.** Nu alleen bij een image-pull-reden; de vraag die beantwoord moet worden is of een nieuwe image ook een OOM-uitschakeling mag opheffen. Argument voor: een nieuwe image kan het geheugenlek juist repareren, en het component blijft anders voorgoed uit. Argument tegen: een OOM keert waarschijnlijk terug en dan flapt hij. Kies expliciet en schrijf de reden erbij; dit is de kern van het plan.
3. **Sleep-mode verzet zijn deadline.** Er is net iets uitgerold, dus de klok naar nul.
4. **De hardgecodeerde `if` uit `project_manager.py`.** Verifiëren: dat bestand noemt `is_image_pull_disable_reason` niet meer.

## Volgorde

1. Het haakpunt, met deployment-gezondheid als eerste bewoner en gedrag ongewijzigd (dus nog steeds alleen image-pull). Verifiëren: hetzelfde component komt terug als daarvoor.
2. De beslissing uit punt 2 nemen en toepassen, met een test die het gekozen gedrag vastlegt inclusief het geval dat je NIET wilt.
3. Sleep-mode aanhaken.
4. De `if` weghalen.

## Waar op te letten

**Dit raakt een gemelde storing.** Als dit de oorzaak is van Maarten-Jans melding, dan is stap 2 de fix en verdient die een aantekening in de release. Vraag eerst wat er bij `disabled-reason` staat, want dan weet je of je het goede probleem oplost.

**Een toestand opruimen is niet hetzelfde als hem negeren.** Een component dat uit staat omdat het geheugen op is, mag niet stilletjes weer aan gaan zonder dat iemand ziet waarom het uit stond. Log wat er opgeruimd wordt en waarom, in dezelfde vorm als de andere diensten dat doen.

**Niet elke dienst wil dit.** Een haakpunt waar niemand op luistert is prima; een haakpunt dat diensten dwingt iets te doen is dat niet. Een dienst die niets met een nieuwe image te maken heeft, hoort de haak gewoon niet te beantwoorden.

**De slaapdeadline verzetten is een keuze, geen automatisme.** Iemand die een image bijwerkt op een slapende deployment wil die misschien juist laten slapen. Bepaal of de deadline verschuift of dat de deployment ook wakker wordt, en schrijf op waarom.
