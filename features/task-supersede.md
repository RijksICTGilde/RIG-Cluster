# Taken die wijken voor een nieuwere taak (supersede)

Een lopende taak breekt zijn ArgoCD-wachtlus af zodra er een nieuwere taak in de wachtrij staat
waarvan de scope die van de lopende taak dekt. De projectfile is op dat moment al gecommit; de
nieuwere taak herverwerkt vanaf die staat, dus doorwachten stelt hem alleen maar uit.

## Het probleem

Het verwerken van een project eindigt in wachten op ArgoCD: wachten tot de `Application` verschijnt,
wachten tot hij gesynchroniseerd is. Die lussen liepen hun volledige timeout uit. Een deployment die
niet goed opstart hield daardoor minutenlang een worker-slot bezet, terwijl de volgende taak voor
datzelfde project ernaast lag te wachten - en die verwerkt het project tóch opnieuw vanaf de
vastgelegde staat.

## Waar de grens ligt

Afbreken mag **pas vanaf de ArgoCD-fase**. Alles daarvoor - de projectfile committen en pushen, de
manifests genereren - moet afmaken: dat is de duurzame staat. Zodra de commit er is, is opgeven
veilig, **mits de nieuwere taak echt hetzelfde werk dekt**.

## De subtiliteit die de eerste poging deed falen

De eerste poging rapporteerde overdrachten als mislukte taken en had een te grove matching. Twee
oorzaken, beide nu opgelost:

1. **Scope is niet af te lezen uit de `deployment_name`-kolom.** Een `add_component` herverwerkt
   alleen `payload.deployment_names` (een lijst) terwijl de kolom NULL is; een `update_component`
   herverwerkt het hele project terwijl de kolom óók NULL is. Een naïeve "NULL = projectbreed"-regel
   laat twee adds op verschillende deployments elkaar inhalen, waarna de inhaler de andere deployment
   niet synct - die blijft ongesynchroniseerd achter.

   Oplossing: `scope_of(task_type, deployment_name, payload)` bepaalt de echte scope. Een nieuwere
   taak haalt de lopende alleen in als **scope(nieuw) ⊇ scope(lopend)** (`covers`). Projectbreed
   (None) dekt alles; een concrete set dekt alleen een deelverzameling, en nooit projectbreed.

   Sinds RC-166 draait `scope_of()` nog maar op één moment in het leven van een taak: bij het
   aanmaken, waarna het antwoord in `async_tasks.affects_deployments` staat. `covers()` en de
   claim-grendel lezen allebei die kolom, dus er is één definitie in plaats van twee die het
   oneens kunnen zijn. Zie `features/taakscope-en-de-uitrolwacht.md`.

2. **`TaskSuperseded` werd opgeslokt.** `process_project` en de handlers hebben brede
   `except Exception`-blokken. Daarom erft `TaskSuperseded` nu van **`BaseException`**, net als
   `asyncio.CancelledError`: `except Exception` vangt hem niet, hij bubbelt naar de worker. Die
   markeert de taak als `completed` met `result.status = "superseded"` - niet `failed`, want er ging
   niets mis. Als `failed` zou het retries en alarmering triggeren voor een bewuste overdracht.

## Hoe het werkt

`opi/core/task_supersede.py`. De worker bindt de identiteit én de berekende scope van de lopende
taak aan een `ContextVar`. De wachtlussen (`argo_manager.wait_for_application_created` en
`wait_for_application_synced`) roepen `raise_if_superseded()` aan; die vraagt de task-service om
nieuwere actieve taken voor het project en gooit `TaskSuperseded` zodra er een de scope dekt.

Een `ContextVar` in plaats van een parameter, omdat de wachtlussen enkele lagen onder de handler
zitten. De vraag gaat naar de **database** (`AsyncTaskService.find_newer_active_tasks`), niet naar een
registry in het geheugen, zodat het blijft werken als API en worker aparte processen zijn.

## Wat een overgenomen taak teruggeeft

De taak eindigt op status `completed` en zijn resultaat vertelt dat hij is overgenomen en door wie:

```json
{
  "status": "superseded",
  "message": "Superseded while waiting for ArgoCD application 'demo-productie' to sync: task 4444... (refresh_project) for project 'demo' covers this task's scope",
  "superseded_by": {
    "task_id": "44444444-4444-4444-4444-444444444444",
    "task_type": "refresh_project",
    "project_name": "demo"
  }
}
```

Het API-antwoord tilt datzelfde blok naar het topniveau, naast `error_message`, als
`superseded_by` (`SupersededByResponse`). Dat gebeurt in `task_response_from_dict()` - het enige
punt waar een opgeslagen taakrecord een API-antwoord wordt, voor V1 en V2 allebei - net als
`error_category`. De sleutel staat er **altijd**, met `null` als er niets is, zodat geen enkele
lezer een extra aanwezigheidscontrole nodig heeft. Er komt geen kolom bij in de database en er is
dus geen migratie.

Drie keuzes daarachter, elk tegen een plausibel alternatief afgewogen:

- **Geen `urls` in een overgenomen resultaat.** Ze zijn op dat moment wél bekend
  (`process_project()` vult `_deployment_results` vóór de waits), maar de sync is precies het stuk
  dat we laten vallen, en de overnemende taak genereert diezelfde manifests zo opnieuw. Urls
  teruggeven zou een toestand beweren die niemand meer gecontroleerd heeft.
  `tests/test_task_supersede.py` pint dat vast met een assertie op de exacte sleutelset.
- **De taakstatus blijft `completed`.** Een vierde eindstatus `superseded` is inhoudelijk zuiverder
  en kost geen migratie, maar de prijs valt buiten deze repo: elke zadctl die vandaag draait pollt
  tot hij een bekende eindstatus ziet, en een onbekende status betekent voor die versies niet
  "klaar" maar doorpollen tot de task-timeout. De eerlijkheid zit daarom in het resultaat en in de
  envelop, niet in de statuskolom.
- **Volgen is een clientkeuze.** De server zegt wat er gebeurde en wie het overnam. Of je daarop
  doorwacht verschilt per aanroeper - een CI-job wil groen worden als het uiteindelijk goed ging,
  een script wil misschien meteen terug - dus die knop hoort in de CLI, niet hier.

Waarom dit nodig was: op 28 augustus 2026 liep in `mpfb-8wh` een deploy via zad-actions rood met
`Could not extract URLs from result`, terwijl de uitrol gewoon was aangemaakt. De deploy had zijn
ArgoCD-wachtstap overgedragen, en het resultaat dat wij teruggaven vertelde dat niet - de CLI las
`completed` als "klaar" en de action zocht urls die er niet waren. De veroorzaker was een
projectbrede taak (handmatig portaalwerk) naast een lopende CI-deploy: twee deployments in
hetzelfde project blokkeren elkaar niet, maar een projectbrede taak droeg geen `deployment_name`
en zag een deployment-gerichte taak dus nooit als in-flight.

Die asymmetrie bestaat niet meer: de claim-grendel vergelijkt sinds RC-166 de opgeslagen scopes
op overlap, en projectbreed overlapt met alles. Twee zulke taken lopen dus niet meer tegelijk, en
supersede blijft doen waar het voor is - een wacht laten wijken voor een taak die het werk
overdoet.

## Wat bewust niet wijkt

De **deletion-wait** (`wait_for_application_deletion`, gebruikt door de delete-flows) wordt **niet**
afgebroken. Een nieuwere taak kan afhankelijk zijn van het voltooien van de verwijdering; een
half-verwijderde app achterlaten is gevaarlijker dan even wachten.

## Robuustheid

- `raise_if_superseded()` staat **buiten** de `try` van de wachtlussen, zodat de brede
  `except Exception` daarin de supersede niet opslokt.
- Faalt de opzoeking zelf (database weg), dan is het antwoord "niet ingehaald" en loopt de wacht
  gewoon door. Een kapotte controle mag nooit een taak laten mislukken.
- Buiten een taak (bijvoorbeeld vanuit een webrequest) is er geen identiteit gebonden en gebeurt er
  niets.

## Tests

`tests/test_task_supersede.py` dekt de scope-bepaling per taaktype, de superset-regel (inclusief dat
disjuncte en smallere scopes níét inhalen), dat `TaskSuperseded` een `BaseException` is en door
`except Exception` heen komt, en dat een falende opzoeking geen supersede is. Daarnaast de keten
zelf: dat de worker een overgenomen taak afrondt met `superseded_by` erin, dat dat resultaat géén
`urls` draagt, dat het API-antwoord de drie velden naar het topniveau tilt (en `null` neerzet voor
een gewone taak), en dat de identiteit van de gevonden taak tot in de envelop niet van naam
verandert. De real-life
sandbox-suite herkent de `superseded`-uitkomst als goedaardig (`sandbox_api.task_outcome`) en telt
hem, in plaats van erover te struikelen.

## Zie ook

- `features/taakscope-en-de-uitrolwacht.md` - de kolom waar `covers()` uit leest, en de
  overlap-grendel die voorkomt dat twee taken op één project elkaar in de weg lopen.
- `features/argocd-token-cache.md` - de andere ingreep op ArgoCD-wachttijd.
- `features/e2e-reallife-tests.md` - de suite die dit gedrag onder gelijktijdige belasting uitoefent.
