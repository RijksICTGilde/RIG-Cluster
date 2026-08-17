# Een wijziging die niemand ziet: de bijlage die niet uitrolt, en de slaapstand die niets zegt

Twee dingen die op het eerste gezicht los van elkaar staan, maar dezelfde vorm hebben: er verandert iets, en de buitenwereld merkt er niets van. Bij de ene is dat een draaiende pod die met verouderde inhoud blijft werken, bij de andere een API die één woord teruggeeft waar drie betekenissen achter zitten.

Beide zijn door de zad-cli gemeld. De diagnoses in dat document kloppen niet helemaal; hieronder staat wat er werkelijk gemeten is, met vindplaats. **Meet het opnieuw voordat je bouwt** — deze metingen zijn van één sessie en van één tak.

## Deel 1: een wijziging in een geheim moet de pod opnieuw starten

### Wat er nu gebeurt

De aanleiding was de bijlage, maar bij het meten bleek het probleem breder. **Geen enkele wijziging die via een Secret binnenkomt bereikt een draaiende pod.** Dat geldt voor bijlagen én voor de env-vars die een gebruiker zelf zet.

De env-vars die letterlijk in de Deployment staan (`manifests/deployment.yaml.jinja:107-112`) zijn niet die van de gebruiker: dat zijn storage-mountpaden, web-URL's en aliassen (`opi/manager/project_manager.py:5596-5628`). De env-vars die een gebruiker zet gaan naar het Secret `{prefix}-user` met een vaste naam en komen binnen via `envFrom: secretRef` (`opi/manager/project_manager.py:5718-5723`, `manifests/deployment.yaml.jinja:121-126`). Die naam verandert nooit, dus de Deployment-spec verandert nooit, dus de pod rolt niet. En `envFrom` wordt alleen bij containerstart geïnjecteerd.

Een bijlage gaat dezelfde weg met een extra val eronder. Hij wordt een Secret met de vaste naam `{deployment}-attch-{id}` (`opi/manager/project_manager.py:5516`, `manifests/binary-secret.yaml.to-sops.jinja`) en wordt met een `subPath` gemount (`manifests/deployment.yaml.jinja:150-155`). **Een `subPath`-mount is een eenmalige kopie bij containerstart.** Kubernetes werkt zo'n bestand principieel nooit bij, ook niet na minuten, ook niet als de Secret allang nieuw is. Waar een gewone Secret-mount na een tijdje vanzelf ververst, gebeurt dat hier dus nooit.

Er staat nergens in `manifests/` een `checksum/`-annotatie, geen `restartedAt`, geen Reloader-annotatie. De enige annotaties op de pod-template zijn die van Prometheus (`manifests/deployment.yaml.jinja:34-40`).

Het enige verschil tussen de twee vandaag zit in het schrijfpad, niet in het manifest. Een env-var-wijziging loopt via een taak met `rollout` en draait `process_project_from_git` (`opi/api/v2/router.py:3717-3768`, `opi/core/task_handlers_components.py:771-787`), dus het Secret in het cluster raakt tenminste bijgewerkt. De vijf attachment-routes komen uit de generieke actie-generator, hebben helemaal geen `rollout`-parameter (`opi/services/catalog/attachments/api.py:343,378`) en de handler doet alleen `save_and_commit_project` (`opi/manager/project_manager.py:7961`) — geen taak, geen reprocess, niets. Een meegestuurde `rollout=true` wordt daar stil genegeerd. De klacht van de CLI staat precies omgekeerd in hun document; ga niet op die diagnose af.

**Verifieer dit alles eerst op het cluster**, want het bepaalt de hele opzet: wijzig een env-var en een bijlage van een draaiende deployment en kijk of de pod herstart en of de nieuwe waarde binnen is. Als er tegen verwachting in wél iets herstart, zoek uit wat dat doet voordat je bouwt.

### Wat er moet komen

1. **Een hash-annotatie op de pod-template**, afgeleid van de inhoud van de geheimen die dit component werkelijk gebruikt: de bijlagen én het user-secret. Verandert de inhoud, dan verandert de spec, dan rolt de pod. Eén mechanisme voor beide; het zijn dezelfde soort wijziging.
2. **Scope het per component.** Een project heeft meerdere bijlagen en meerdere componenten. Alleen wat deze inhoud gebruikt hoort te herstarten. Een projectbrede hash die alles laat rollen bij elke wijziging is een regressie, geen oplossing.
3. **De attachment-routes moeten werkelijk uitrollen.** Zonder dat verandert de annotatie pas als iets ánders het project verwerkt, en dan is de winst een toevalligheid. Dit verandert hun contract van synchroon 200/201 naar 202 met een task-id, zoals de web-UI dat voor verwijderen al doet (`TaskType.DELETE_ATTACHMENT` bestaat, `opi/core/async_task_service.py:55`). **Beschrijf die contractwijziging expliciet in de PR**, en geef `rollout` daar de gewone betekenis die hij elders heeft.
4. **Determinisme.** De hash moet stabiel zijn over herhaalde renders van ongewijzigde inhoud, anders churnt de GitOps-repo bij elke verwerking. Dat is eerder misgegaan met SOPS (`features/sops-skip-unchanged-reencryption.md`), dus meet het: twee keer genereren zonder wijziging geeft een identiek bestand.
5. **Let op wat je in de annotatie zet.** Een hash, nooit inhoud. De bron is een geheim en de pod-template staat in een repository.

### Wat er buiten valt, tenzij het gratis meekomt

De platformgeheimen die OPI zelf genereert (databasewachtwoorden en dergelijke) hangen aan dezelfde `envFrom`-constructie en hebben dus hetzelfde gedrag. Als het mechanisme dat je bouwt daar vanzelf op past, meld dat en meet of het geen ongewenste herstarts oplevert bij de gewone verwerking. Ga het niet apart oplossen in deze taak.

## Deel 2: de slaapstand zegt één woord met drie betekenissen

### Wat er nu gebeurt

De twee endpoints in `opi/services/catalog/sleep_mode/router.py` geven een kale `JSONResponse` terug, zonder response-model. In `/openapi.json` staat dus geen schema, geen enum, geen omschrijving. Een gegenereerde client weet niets.

Erger is dat er twee woordenboeken bestaan die allebei `state` heten, op naburige endpoints in dezelfde router:

- `GET .../status` geeft `starting | ready`. Dat is het pollcontract van het waker-image, dat alleen kijkt of het woord `ready` is (`images/zad-waker/main.go:162`).
- `POST .../wake` geeft `awake | sleeping | waking`, de echte slaaptoestand uit `opi/services/catalog/sleep_mode/state.py`.

En de kapotte kant die de CLI meldde zit in `opi/services/catalog/sleep_mode/flow.py:282-284`: staat sleep-mode uit, dan is er geen waker-component en komt er hardgecodeerd `starting` uit, zonder ooit naar een pod of naar de opgeslagen toestand te kijken. Vandaar dat de CLI altijd `starting` zag.

### Wat er moet komen

1. **Response-modellen met echte enums op beide endpoints**, zodat de waarden in `/openapi.json` staan met een omschrijving per waarde. Volg het patroon dat er al is voor `domain-format` en de keuzelijsten (`opi/api/openapi_choices.py`), zodat het er niet als een tweede manier bij komt te staan.
2. **`state` op `/status` blijft byte-identiek.** Dat is het contract van een waker-image dat los uit de registry wordt gepulld en dus ouder kan zijn dan deze code. Niet aanraken.
3. **Een tweede veld ernaast met de echte slaaptoestand**: `awake | sleeping | waking`, plus `disabled` als sleep-mode voor dit project uit staat. Additief, dus geen enkel risico voor een draaiende waker, en de client krijgt eindelijk wat hij uitleest.
4. **De hardgecodeerde `starting` weg** ten gunste van dat nieuwe veld: sleep-mode uit is `disabled` en geen enkele pod hoeft daarvoor bevraagd te worden.

Overweeg of `wake` zijn `state` ook onder dat tweede veld hoort te rapporteren zodat één woord één betekenis heeft, maar **breek het bestaande antwoord niet** — er hangt een waker aan.

## Verifieerbaar

- Een test die aantoont dat het overschrijven van een bijlage de pod-spec verandert, en een die aantoont dat een ongewijzigde bijlage dat níet doet. Beide moeten rood staan zonder de wijziging. Idem voor een env-var.
- Op het cluster gemeten: bijlage vervangen, pod herstart, nieuwe inhoud staat in het bestand. En hetzelfde voor een env-var. Dit is het punt waarop de `subPath`-aanname zich bewijst of onderuit gaat; meld wat je zag.
- `/openapi.json` bevat voor beide sleep-mode-endpoints een schema met de enumwaarden en een omschrijving per waarde.
- Sleep-mode uit geeft `disabled` in het nieuwe veld en `starting` in het oude.
- `uv run pytest tests/ -q` groen, plus `ruff check`, `ruff format`, `pyright`.
