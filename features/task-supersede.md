# Taken die wijken voor een nieuwere taak (supersede)

Een lopende taak breekt zijn ArgoCD-wachtlus af zodra er een nieuwere taak voor hetzelfde project of
dezelfde deployment in de wachtrij staat.

## Het probleem

Het verwerken van een project eindigt in wachten op ArgoCD: wachten tot de `Application` verschijnt,
of tot hij verdwenen is. Die lussen liepen hun volledige timeout uit. Een deployment die niet goed
opstart hield daardoor minutenlang een worker-slot bezet, terwijl de volgende taak voor precies dat
project ernaast lag te wachten.

Dat is dubbel zonde, want die nieuwere taak verwerkt het project tóch opnieuw vanaf de vastgelegde
staat. Het uitzitten van de timeout stelt hem alleen maar uit.

## Waar de grens ligt

Afbreken mag **pas vanaf de ArgoCD-fase**. Alles daarvoor - de projectfile committen en pushen, de
manifests genereren - moet afmaken: dat is de duurzame staat. Zodra de commit er is, is opgeven
veilig, want de nieuwere taak leest precies die commit.

Dat is dezelfde redenering die de volgorde binnen een taak goed maakt: eerst git, dan ArgoCD.

## Hoe het werkt

`opi/core/task_supersede.py`.

De worker bindt de identiteit van de lopende taak (`task_id`, project, deployment) aan een
`ContextVar`. De wachtlussen roepen `raise_if_superseded()` aan, die aan de task-service vraagt of er
een nieuwere taak is. Zo ja, dan gooit hij `TaskSuperseded`.

Een ContextVar in plaats van een extra parameter, omdat de wachtlussen enkele lagen onder de handler
zitten; die parameter door de hele keten rijgen zou veel meer code raken dan het gedrag rechtvaardigt.

De vraag gaat naar de **database** (`AsyncTaskService.find_superseding_task`), niet naar een
registry in het geheugen. Daardoor blijft het werken als de API en de worker aparte processen zijn.

### Wanneer een taak wijkt

Een nieuwere taak voor hetzelfde project haalt de lopende in wanneer:

- hij projectbreed is (geen deployment), dus alle deployments raakt, of
- hij dezelfde deployment betreft, of
- de lopende taak zelf projectbreed is, en dus alles overlapt.

### Eindstatus

Een ingehaalde taak wordt afgerond als `completed` met `{"status": "superseded"}` in het resultaat -
**niet** als `failed`. Er is niets misgegaan: de projectfile is gecommit en het resterende werk wordt
overgenomen. Als `failed` zou het retries en alarmering triggeren voor een bewuste overdracht.

## Robuustheid

- `raise_if_superseded()` wordt **buiten** de `try` van de wachtlussen aangeroepen, zodat de brede
  `except Exception` daarin de supersede niet opslokt en als "even opnieuw proberen" behandelt.
- Faalt de opzoeking zelf (database weg), dan is het antwoord "niet ingehaald" en loopt de wacht
  gewoon door. Een kapotte controle mag nooit een taak laten mislukken.
- Buiten een taak (bijvoorbeeld vanuit een webrequest) is er geen identiteit gebonden en gebeurt er
  niets.

## Tests

`tests/test_task_supersede.py` dekt: geen nieuwere taak, wél een nieuwere taak, draaien buiten een
taak, een falende opzoeking, en dat de volledige identiteit aan de query wordt meegegeven.

## Zie ook

- `features/argocd-token-cache.md` - de andere ingreep op ArgoCD-wachttijd.
