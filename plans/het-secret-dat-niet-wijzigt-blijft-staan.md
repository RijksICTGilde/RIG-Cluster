# Het secret dat niet wijzigt, blijft staan

**Status**: plan, nog niets gebouwd.
**Datum**: 2026-09-17
**Aanleiding**: issue #153, nagemeten op main op 17-09-2026. De acute bug uit die issue is opgelost, de twee beslispunten staan nog open. Het tweede punt is inmiddels geen theorie meer: een klant meldt dat iedereen op de demo-console uitgelogd raakt zodra er een deploy is geweest.

## Wat er gebeurt

Een deployment schrijft zijn secrets, gooit daarna op zijn manifestmap alles weg wat deze run niet opnieuw is gemaakt, en versleutelt pas als laatste stap. In die volgorde zit het probleem: de prune verwijdert de ciphertext van de vorige run, en de skip-if-unchanged die daarna draait heeft dus niets meer om tegen te vergelijken.

Dit is geen samenloop van twee schrijvers maar een vaste volgorde binnen één run. Genereren gebeurt in `create_application_manifests`, prunen op `project_manager.py:4097` en `:4104`, versleutelen op `:4142`. `_sops_plaintext_unchanged` opent met `if not os.path.exists(encrypted_path): return False` (`opi/utils/sops.py:116`), dus een weggepruned bestand betekent altijd opnieuw versleutelen, en SOPS is niet deterministisch. Elke run een nieuwe diff.

De prune doet dat omdat `_write_secret_file` alleen de `.to-sops.yaml` in `created_files` zet (`project_manager.py:1599`, en net zo op `:5101`, `:6671`, `:6740`). De bijbehorende `.sops.yaml` staat niet in de gewenste toestand, draagt wel een componentprefix, en wordt dus als overbodig aangemerkt. Nagemeten met de echte helper:

```
generated_files = {fundament-deployment.yaml,
                   fundament-user-secret.to-sops.yaml,
                   productie-fundament-oauth2-cookie-secret.to-sops.yaml}
-> ['fundament-user-secret.sops.yaml',
    'productie-fundament-oauth2-cookie-secret.sops.yaml']
```

Geraakt worden elk `<component>-user-secret`, het oauth2-cookie-secret, en via de prune op dienstniveau (prefix `<deployment>-<dienst>-`) ook `<deployment>-keycloak-secret` en `<deployment>-redis-secret`. De database- en minio-secrets ontsnappen alleen omdat hun bestandsnaam toevallig geen dienstprefix draagt. Op projectniveau is het al opgelost, en precies zoals het hoort: daar zet `project_manager.py:4004` beide namen in `created_files`, met de reden erbij. Die kennis is nooit naar de andere twee prunes gelopen.

Het tweede punt staat los van de volgorde. `opi/services/catalog/authorization_wall/__init__.py:183` maakt het cookie-secret met `secrets.token_urlsafe(32)` in `contribute_manifest_context`, en die draait bij elke run. De plaintext wijzigt dus echt, waarmee ook een gerepareerde skip-check niets meer te sparen heeft, en elke reprocess of refresh van een project gooit alle lopende oauth2-proxy-sessies eruit. Gebruikers worden uitgelogd omdat iemand een veld in de portal heeft aangepast. Dat is precies de klacht van de demo-console, en daarmee is dit punt niet langer een openstaande ontwerpvraag maar een storing met een melder.

## Wat we bouwen

**1. De ciphertext van een secret dat deze run opnieuw gemaakt wordt, overleeft de prune.**

De regel is in beide selectie-helpers dezelfde: staat `<base>.to-sops.yaml` in `generated_files`, dan is `<base>.sops.yaml` niet overbodig maar de vorige versie van datzelfde secret, en die blijft staan tot de encryptie erover gaat. Zet die regel op één plek en laat `_select_obsolete_component_manifests` en `_select_obsolete_service_manifests` er allebei door lopen.

Daarmee wordt de dubbele registratie op `project_manager.py:4004` overbodig: het projectniveau loopt door dezelfde selector. Haal die weg en laat de uitleg die daar staat mee verhuizen naar de helper, zodat er één mechanisme is in plaats van twee die hetzelfde bedoelen. Blijkt tijdens het bouwen dat het projectniveau een andere selector gebruikt dan gedacht, zeg dat dan in de PR en laat `:4004` staan.

**2. Het cookie-secret blijft staan zolang het bestaat.**

Het nieuwe gedrag: bestaat er al een versleuteld cookie-secret voor deze deployment, dan neemt de schrijver die waarde over in plaats van een nieuwe te genereren. De plaintext wijzigt dan niet meer, en met punt 1 erbij blijft ook de ciphertext ongemoeid.

De plek is de gedeelde schrijver `_write_secret_file`, niet de dienst. Een dienst zegt wat zijn secret is, niet hoe de vorige waarde van schijf komt. Dus:

- `SecretFileSpec` krijgt een veld waarmee een dienst zegt dat de bestaande waarde voorgaat op de nieuw aangeleverde. Naam is een voorstel, kies wat bij de andere velden past; `authorization-wall` is voorlopig de enige die hem zet.
- `_write_secret_file` zoekt `<manifest_name>.sops.yaml` in zijn eigen `output_dir`, ontsleutelt die met de projectsleutel, en neemt per sleutel uit `secret_pairs` de bestaande waarde over als die er staat. De schrijver draait vóór de prune, dus hij ziet de ciphertext van de vorige run sowieso.
- Ontsleutelen gaat via de bestaande route in `opi/utils/sops.py` (`_decrypt_sops_with_key`, nu nog privé). Maak die publiek in plaats van een tweede ontsleutelweg ernaast te zetten.
- De projectsleutel komt van `self._sops_private_key_for(project_data)`, één keer opgehaald en meegegeven aan de schrijver.

Dit faalt naar de nieuwe waarde toe, nooit naar een exception: geen bestand, geen sleutel, een mislukte ontsleuteling of een document zonder die sleutel betekent gewoon de vers gegenereerde waarde. Dat is exact het gedrag van vandaag, dus het slechtste geval is het huidige geval.

De consequentie hoort in de PR genoemd: het cookie-secret rouleert hierna niet meer vanzelf. Wie hem wil vervangen, verwijdert de waarde en laat hem opnieuw aanmaken. Dat is een bewuste handeling in plaats van een bijwerking van elke deploy.

## Wat we niet doen

De waarde uit het cluster lezen in plaats van uit git. Git is de waarheid voor deployments, en een lookup langs de cluster-API zou dat omdraaien.

Een rotatiemechanisme bouwen voor het cookie-secret. Niet gevraagd, en zolang niemand erom vraagt is handmatig weghalen genoeg.

`secrets.token_urlsafe(32)` vervangen. Die blijft de generator voor de eerste keer.

De andere secrets als hergebruik markeren. Hun plaintext is al stabiel; die hebben aan punt 1 genoeg.

Locking of enige andere vorm van samenloopbescherming. Er is hier geen tweede schrijver.

## Klaar als

- De prune laat `<base>.sops.yaml` staan zolang `<base>.to-sops.yaml` deze run gemaakt is, getoetst op allebei de selectie-helpers, met de meting uit dit plan als testgeval.
- De bestaande prunetests blijven groen, inclusief die op een verwijderd component: van een component dat weg is moeten beide vormen alsnog verdwijnen.
- Een test op de schrijver: bestaat er een versleuteld cookie-secret, dan draagt de geschreven plaintext de oude waarde; bestaat het niet, dan een nieuwe. Plus een faaltest: onleesbare of niet te ontsleutelen ciphertext levert de nieuwe waarde op en geen exception.
- Op de sandbox: een project met een auth-wall twee keer achter elkaar reprocessen zonder inhoudelijke wijziging levert in `zad-deployments` bij de tweede run geen enkele diff op een `*.sops.yaml` op. Dit is de eigenlijke acceptatie; de units toetsen de onderdelen, dit toetst de afspraak.
- `uv run ruff check . --fix`, `uv run ruff format .` en `uv run pyright` schoon, unitsuite groen.
- `features/sops-skip-unchanged-reencryption.md` vertelt nu ook hoe de prune zich tot de skip verhoudt, en dat het cookie-secret zijn waarde behoudt.
- De PR benoemt punt A en punt B uit #153 en sluit ze.
