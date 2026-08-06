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

1. **Een haakpunt voor een expliciete gebruikersactie op een deployment**, breder dan alleen de image: een upsert op een deployment telt net zo goed. Noem het daarnaar, want een haak die "de image is vervangen" heet krijgt de volgende actie er als uitzondering bij. Zelfde vorm als de bestaande twee haakpunten, zodat er niets nieuws te leren valt.
2. **Deployment-gezondheid heft de uitschakeling op, ongeacht de reden.** Beslist door de opdrachtgever op 6 augustus: een expliciete actie heft ALTIJD de vorige toestand op, dus ook een OOM- of crashloop-uitschakeling en niet alleen image-pull. De redenering: de nieuwe image of de upsert is het signaal dat de oude situatie niet meer geldt. Bouw hier dus geen uitzonderingen en geen reden-specifieke `if`. Keert het probleem terug, dan zet de watcher hem gewoon opnieuw uit; dat is zichtbaar en herstelbaar, terwijl een component dat voorgoed uit blijft dat niet is.
3. **Sleep-mode gaat naar wakker.** Niet alleen de deadline verzetten: slapend wordt awake. Er is net iets uitgerold, dus de deployment hoort te draaien.
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

**De regel is zonder uitzonderingen, en dat is het punt.** De verleiding is om per geval te bedenken of opheffen wel verstandig is, en dan staat er over een half jaar weer een lijst met redenen in `project_manager.py`. Keert een probleem terug, dan zet de watcher het component opnieuw uit; dat is zichtbaar en herstelbaar. Een component dat voorgoed uit blijft omdat niemand de juiste reden noemde, is dat niet.
