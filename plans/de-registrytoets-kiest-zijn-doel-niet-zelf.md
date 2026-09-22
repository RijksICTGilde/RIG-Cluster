# De registrytoets kiest zijn doel niet zelf, en draagt zijn wachtwoord niet in de argv

Status: plan, 17 september 2026. Niet gebouwd. Voegt de taken RC-179 en RC-180 samen: dat waren twee beschrijvingen van dezelfde bevinding uit de securityreview van RC-177 (PR #165), beide niet-blokkerend bevonden.

RC-177 is inmiddels gemerged, dus dit staat nu op main en niet meer in een PR.

## Wat er is, gemeten

`SkopeoConnector.check_repository_access` (`opi/connectors/skopeo.py:187`) is het eerste uitgaande verzoek naar een host die de AFNEMER kiest. Twee dingen mankeren eraan.

### 1. Het doel wordt niet begrensd

De enige horde is `UPSTREAM_PATTERN` (`opi/services/catalog/image_registries/config_model.py:20`), en die eist alleen een punt in de hostnaam, of een poort, of het woord `localhost`:

```
^(?:LABEL(?:\.LABEL)+(?::PORT)?|LABEL:PORT|localhost)(?:/...)*$
```

Daar komen `localhost:9595`, `10.43.0.1:8080`, `127.0.0.1:22`, `kubernetes.default.svc` en `169.254.169.254` allemaal langs. Het patroon is een vormcontrole, geen bestemmingscontrole, en dat is precies wat het volgens zijn eigen comment ook wil zijn.

Het wordt een leesprimitief doordat het antwoord terugkomt: de eerste stderr-regel gaat als veldfout naar de gebruiker. Het verschil tussen `connection refused`, een TLS-fout en een 401 is daarmee een bruikbaar orakel om vanuit de OPI-pod het clusternetwerk af te tasten.

### 2. Het wachtwoord staat in de procestabel

```python
cmd = ["skopeo", "list-tags", "--creds", f"{username}:{password}", f"docker://{repository}"]
```

Zichtbaar in `/proc/<pid>/cmdline` zolang het proces leeft. Log en foutmelding zijn wel gemaskeerd (`_mask_list_tags_credentials`), dus het lek zit uitsluitend in de argv.

**Let op, de oorspronkelijke bevinding klopt hier niet.** RC-180 zegt: "Een authfile haalt het weg; dezelfde vorm zit al in `push_image`." Dat is niet zo. `_build_command` zet `--dest-creds` net zo goed op de commandoregel (`skopeo.py:169`), en er is nergens in het bestand een `authfile` of `REGISTRY_AUTH_FILE`. Er is dus geen bestaande vorm om na te volgen; die moet in deze taak gemaakt worden, en beide aanroepen horen hem te gebruiken.

## Wat er moet gebeuren

### 1. Een weigerlijst op de bestemming

Niet in `UPSTREAM_PATTERN`: dat is een vormcontrole voor het projectbestand en moet dat blijven. De grendel hoort bij de handeling die naar buiten gaat.

Los de hostnaam op en weiger het verzoek als een van de adressen in private of bijzondere ruimte valt: loopback, link-local (`169.254.0.0/16`, dus ook het metadata-adres), de RFC1918-blokken, unique-local en link-local in IPv6. Python's `ipaddress`-module heeft daar `is_private`, `is_loopback` en `is_link_local` voor; schrijf geen eigen reeksen uit.

Twee dingen om niet te vergeten:

- **Namen die geen IP zijn.** `kubernetes.default.svc` haalt het patroon en is geen adres. Resolveren en dan pas oordelen is de enige manier om die te vangen.
- **De uitkomst mag niet verklappen wat de fout was.** Geef één vaste melding terug voor een geweigerde bestemming. Doe je dat niet, dan is de weigering zelf weer een orakel.

Ga na of dezelfde grendel bij `repositories[].url` hoort. Dat is een tweede weg naar dezelfde soort bestemming.

### 2. Credentials uit de argv

Geef skopeo een authfile mee in plaats van `--creds` en `--dest-creds`. Het bestand krijgt mode 0600, staat in een tijdelijke map en wordt na afloop opgeruimd, ook als het commando faalt.

Doe dit voor **beide** aanroepen, `check_repository_access` en `push_image`, en gebruik er één helper voor. Twee eigen implementaties is precies hoe de ene over een jaar wel een tijdelijk bestand opruimt en de andere niet.

De maskering in de logging blijft staan zoals hij is.

## De toets

- `169.254.169.254`, `127.0.0.1:22`, `10.43.0.1:8080` en `kubernetes.default.svc` worden geweigerd, elk met dezelfde melding;
- een gewone publieke registry (`ghcr.io/iets`, `code.overheid.nl/iets`) wordt niet geweigerd;
- een naam die naar een privaat adres resolveert wordt geweigerd, ook al ziet de naam zelf er publiek uit. Dit is de test die een implementatie op alleen de tekst laat vallen;
- `grep -n "\-\-creds\|--dest-creds" opi/connectors/skopeo.py` levert niets meer op;
- tijdens een aanroep staat het wachtwoord niet in de argv van het subproces, en na afloop bestaat het authfile niet meer, ook niet als skopeo een foutcode gaf;
- `UPSTREAM_PATTERN` is ongewijzigd: de vormcontrole op het projectbestand is niet de plek van deze grendel;
- de bestaande maskering in de logging werkt nog.

## Waar op te letten

**Dit is een grendel op een bestaand pad, geen nieuwe functie.** Wie hem te streng zet, breekt het opgeven van een registry voor iedereen. Wie hem te los zet, verandert niets. De testset hierboven is daarom de maat, niet het gevoel.

**Een sandbox-registry draait op `localhost:5001`.** Sinds RC-193 staat er een registry naast het kind-cluster, en die is in de sandbox een geldige bestemming. Ga na of deze toets daar langskomt voordat je loopback onvoorwaardelijk weigert, en maak er zo nodig een expliciete uitzondering van die alleen in de sandbox geldt. Een grendel die het lokale ontwikkelpad sloopt, wordt uitgezet en beschermt daarna niets meer.
