# De API schrijft versioning aan terwijl niemand daarom vroeg

Status: plan, 13 augustus 2026. Een project dat via de API is aangemaakt draagt dit, zonder dat de aanroeper het meestuurde:

```yaml
- name: minio-storage
  config:
    enable-versioning: true
```

Dat hoort er niet te staan, en zeker niet op `true`.

## Wat al is nagegaan, zodat je daar niet opnieuw begint

* **Het is geen standaard van ons.** `MinioStorageConfig.enable_versioning` heeft `default=None` (`opi/services/catalog/minio/config_model.py:25`), en de provisioner leest hem als `config.get("enable-versioning", False)` (`opi/manager/minio_manager.py:131`). Afwezig betekent dus UIT.
* **De wizard schrijft hem niet.** Het aanvinkvakje laat bij niet-aanvinken bewust geen sleutel achter in plaats van `false` weg te schrijven; dat staat als toelichting in `opi/services/catalog/minio/editables.py:19`.
* **`add_services_to_project` schrijft hem niet.** Die zet kale strings in `project_data["services"]` (`opi/services/services.py`, rond regel 695).
* **Niets in de applicatie schrijft de waarde `true`.** Een zoektocht door `opi/` levert alleen leesplekken op, plus een docstringvoorbeeld in `minio_manager.py:61`.
* **De vorm is een aanwijzing.** Er staat `name:` en niet `reference:`, met een `config`-blok eronder. Dat is de vorm die `Service.implicit_project_entry()` oplevert (`opi/services/catalog/base.py:939`), de haak uit RC-84 waarmee een dienst zichzelf op projectniveau aanmeldt. Minio heeft `allows_implicit_project_selection = True` en overschrijft `implicit_project_config()` **niet**, dus die zou een KALE naam moeten opleveren en geen configblok.

Daar zit dus de verdenking: ergens tussen `implicit_project_entry()` en het projectbestand ontstaat een configblok met een waarde die niemand heeft gezet. Kijk naar het staartje van die functie, waar de entry tegen het projectlaagmodel wordt gevalideerd: een `model_dump()` zonder uitsluiting materialiseert velden die alleen een default hebben, en dan verschijnt een sleutel die in de invoer niet stond. Dat verklaart de vorm; het verklaart nog niet de waarde `true`, en juist dat is het stuk dat je moet meten.

## Wat er moet gebeuren

1. **Meet waar `true` vandaan komt.** Maak een project via de API met minio en kijk wat er in het projectbestand belandt. Niet redeneren vanuit de code: dit is drie keer eerder de valkuil geweest.
2. **Repareer het bij de bron.** Een dienst die zichzelf aanmeldt hoort geen keuze voor de gebruiker te maken. "Niet gezet" is hier betekenisvol en iets anders dan `false`: het betekent "volg de platformstandaard".
3. **Kijk of het breder is.** Als een `model_dump()` de oorzaak is, dan raakt dit elke dienst die zichzelf mag aanmelden en niet alleen minio. Loop de veertien langs.

## Waarom dit meer is dan een cosmetisch veld

Versioning aanzetten is niet gratis: elke overschrijving bewaart de vorige versie, dus het bucketgebruik groeit met elke schrijfactie in plaats van gelijk te blijven. Een gebruiker die dit niet koos, betaalt er wel voor en ziet niet waarom.

En het bredere punt: een impliciete aanmelding die stilzwijgend een instelling zet, is precies wat het plan van RC-84 wilde voorkomen. Daar staat het letterlijk: een dienst die per ongeluk ontstaat met een verzonnen standaard is erger dan een foutmelding.

## De toets

- een project dat via de API met minio wordt aangemaakt draagt GEEN `enable-versioning`, tenzij de aanroeper hem meestuurde;
- een aanroeper die hem wél meestuurt krijgt zijn waarde, ook `false`;
- bestaande projecten met een ongevraagde `true` zijn benoemd: hoeveel het er zijn en wat ermee gebeurt (laten staan is een geldig antwoord, maar zeg het);
- als de oorzaak breder is dan minio, is dat voor alle diensten die zichzelf mogen aanmelden nagegaan.
