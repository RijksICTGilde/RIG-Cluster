# De ontsleuteling hoort in het proces, niet in een fork

Status: plan, 22 september 2026. Niet gebouwd. Hoort bij issue #145.

Aanleiding: issue #145 zegt dat de projectdetailpagina seconden per load kost door age-decryptie per veld. Dat is nagemeten en **de snelheidsclaim klopt niet meer**. Wat er wel staat is een andere kost, die het ticket niet noemt en die zwaarder weegt.

## Wat er gemeten is

`decrypt_age_content` (`opi/utils/age.py:23`) schrijft de private sleutel naar een tempfile en start per aanroep het `age`-binary als subprocess. Gemeten op deze machine, met een echte sleutel en een echt versleuteld blok:

```
  1 veld  sequentieel:     6.2 ms   (6.2 ms per veld)
 10 velden sequentieel:   59.0 ms   (5.9 ms per veld)
 40 velden sequentieel:  243.6 ms   (6.1 ms per veld)
 40 velden parallel:     101.9 ms
```

Een detailpagina ontsleutelt de projectsleutel, de api-key, per Keycloak-realm het wachtwoord en het OTP-geheim, en per houder het `user-env-vars`-blok. Dat laatste is **één** decrypt per houder en niet één per variabele (`services/project_env_vars.py:25` ontsleutelt het hele blok in één keer). Een project met twee realms en tien componenten komt daarmee op ongeveer zestien decrypts, dus rond de 100 ms.

Om aan één seconde te komen zijn ruim 160 versleutelde blokken nodig. Dat is geen realistisch project, en de waarneming van vandaag is dat de pagina niet traag aanvoelt.

**De tweede oorzaak uit het ticket is intussen weg, en dat verklaart het verschil.** Issue #145 noemde naast de decryptie ook `ensure_projects_fresh()` op het request-pad: bij een verlopen TTL van 30 seconden deed de eerstvolgende pagina synchroon een git fetch, leegde de cache en herlas alle projectbestanden, terwijl gelijktijdige verzoeken op een lock wachtten. Dat is de kost die seconden kan duren, en hij is met de ProjectStore verdwenen (`opi/core/startup.py:255` legt uit waarom). Wat overblijft is de decryptie, en die kost milliseconden.

## Waarom het toch de moeite waard is

Niet de tijd, maar de **fork per veld**. Elke decrypt is een proces erbij, met een eigen adresruimte en een tempfile met de private sleutel erin. OPI is eerder OOMKilled door precies deze klasse: subprocessen die uitwaaieren en pieken vormen die tussen twee scrapes door onzichtbaar blijven. De code meet het zelf al, via `track_subprocess_memory("age")` en de metric `opi_connector_subprocess_calls_total{connector="age"}`.

Een detailpagina doet die forks bij elke render, voor elke gelijktijdige bezoeker. Tegelijk staat er een gratis alternatief.

## Het alternatief, ook gemeten

`pyrage`, dat issue #145 zelf al als eerste voorgestelde fix noemt, is een Python-binding op de Rust-implementatie van age en doet dezelfde ontsleuteling in het proces zelf:

```
 10x in-process:    0.60 ms   (0.060 ms per veld)
 40x in-process:    2.27 ms   (0.057 ms per veld)
400x in-process:   21.21 ms   (0.053 ms per veld)
```

**107 keer sneller**, en de uitkomst is byte-voor-byte dezelfde als die van het binary; getoetst op een ASCII-armored blok, de vorm die OPI gebruikt. Geen fork, geen tempfile, en de private sleutel blijft in het geheugen van het proces in plaats van op schijf te staan.

Dat laatste is een tweede winst die los staat van snelheid: `tempfile.NamedTemporaryFile` zet de sleutel in `/tmp`, en al wordt hij direct opgeruimd, hij staat er tijdens elke decrypt.

## Wat er moet gebeuren

1. **Voeg `pyrage` toe als dependency** en zet `decrypt_age_content` erop om, met dezelfde handtekening en hetzelfde gedrag bij een fout. De functie blijft `async` zodat geen enkele aanroeper hoeft te veranderen, ook al is er niets meer om op te wachten. *Verify:* de bestaande tests op `opi/utils/age.py` blijven groen zonder aanpassing.
2. **Toets beide richtingen tegen het binary.** Een blok versleuteld met `age` moet door `pyrage` te lezen zijn en andersom, want bestaande projectbestanden en SOPS-bestanden zijn met het binary gemaakt. *Verify:* een test die een met `age` versleuteld blok ontsleutelt via de nieuwe weg, en een met de nieuwe weg versleuteld blok via `age -d`.
3. **Laat de encryptiekant met rust, of doe hem in dezelfde stap.** `_encrypt_with_age_and_base64encode_as_prefixed_string` start ook een subprocess. De winst is daar kleiner, want encryptie gebeurt bij het opslaan en niet bij elke render. Doe het alleen als stap 2 laat zien dat beide richtingen uitwisselbaar zijn.
4. **Behoud de metric.** `track_subprocess_memory("age")` heeft geen betekenis meer zonder subprocess, maar het aantal aanroepen blijft interessant. Kies bewust: de teller behouden onder een andere naam, of hem laten vervallen en dat opschrijven. *Verify:* `opi_connector_subprocess_calls_total{connector="age"}` daalt naar nul in de sandbox, en dat is het bewijs dat het pad echt om is.
5. **Geen cache.** Het ticket stelt caching voor als fix 2. Dat is hier de verkeerde oplossing: ontsleutelde geheimen in een cache leggen is een risico dat je terugkrijgt voor tijdwinst die je met stap 1 al hebt. Dit staat expliciet in het plan zodat niemand het later alsnog "erbij" doet.

## Assertie

```bash
cd operations-manager/python
uv run pytest tests/ -k "age or encrypt or decrypt" -x -q --tb=short
uv run ruff check . --fix && uv run ruff format . && uv run pyright
```

Klaar als:

- een met het `age`-binary versleuteld blok door de nieuwe weg gelezen wordt, en andersom;
- geen enkele aanroeper van `decrypt_age_content` is aangepast;
- er geen `age`-subprocess meer start op het leespad, aantoonbaar via de teller;
- de private sleutel niet meer naar schijf wordt geschreven om te kunnen ontsleutelen.

## Wat dit NIET is

Dit is geen prestatiefix, want de pagina is niet traag. Het is het wegnemen van een fork per veld op een dienst die eerder aan subprocess-uitwaaiering is bezweken, met een snellere en veiligere weg die toevallig ook eenvoudiger is.

Niet gemeten: hoeveel `age`-aanroepen productie werkelijk doet. De metric bestaat (`opi_connector_subprocess_calls_total{connector="age"}`), maar `kubectl exec` op de productiepod is geblokkeerd en via Grafana is het niet opgehaald. Dat cijfer zou de omvang bevestigen, en het is een eerste stap waard voor wie dit oppakt.
