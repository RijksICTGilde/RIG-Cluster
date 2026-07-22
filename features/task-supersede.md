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
`except Exception` heen komt, en dat een falende opzoeking geen supersede is. De real-life
sandbox-suite herkent de `superseded`-uitkomst als goedaardig (`sandbox_api.task_outcome`) en telt
hem, in plaats van erover te struikelen.

## Zie ook

- `features/argocd-token-cache.md` - de andere ingreep op ArgoCD-wachttijd.
- `features/e2e-reallife-tests.md` - de suite die dit gedrag onder gelijktijdige belasting uitoefent.
