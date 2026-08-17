# Terugzetten in je eigen database, zonder een wachtwoord dat je niet hebt

Status: plan, 12 augustus 2026. Aanleiding: vraag 7 uit `plans/vragen-uit-zad-cli.md`, gesteld door het zad-cli-project. Onder die vraag staat `<!-- ruimte voor RIG-Cluster -->`; dit plan vult die plek en lost hem meteen op.

## Wat er nu is, gemeten

`DatabaseRestoreRequest` in `opi/api/restore_router.py:240` eist vier velden, en `BucketRestoreRequest` doet hetzelfde voor MinIO:

```
target_database_host, target_database_name, target_database_user, target_database_password
target_minio_endpoint, target_bucket_name, target_access_key, target_secret_key
```

Voor een **externe** bestemming is dat precies goed: je zegt waar het heen moet en met welke sleutel.

Maar de gewone handeling is terugzetten in de database van je **eigen** project, en die gegevens beheert het platform. Ze worden in de container geïnjecteerd en de gebruiker ziet ze nergens. De CLI heeft nagekeken dat ze niet in de dienstconfiguratie van `postgresql-database` staan, niet in `project describe` en niet in `env list` (dat geeft de namen van de variabelen van de gebruiker, niet de geïnjecteerde platformwaarden).

**Gevolg:** `zad restore database` en `zad restore bucket` zijn wel te bouwen maar niet te draaien voor het normale geval. In hun draaiboek staat de stap nu als niet-uitvoerbaar.

## Wat er moet gebeuren

**De doelvelden worden optioneel, en bij afwezigheid zet het platform terug in de dienst van het project waar de API-sleutel bij hoort.**

Dat is optie 1 van de drie die de CLI voorstelt, en zij hebben er zelf een voorkeur voor met een goede reden: er gaan dan geen credentials over de lijn. Optie 2, een endpoint dat de verbindingsgegevens teruggeeft, maakt van een wachtwoord dat nu alleen in de pod staat iets dat over de API opvraagbaar is. Dat is een echte verruiming van wat er te halen valt met een gestolen sleutel, en die ruilen we niet in voor gemak. Optie 3, een apart "restore in place"-endpoint, verdubbelt een pad dat verder identiek is.

Concreet:

1. **De vier doelvelden krijgen `default=None`** in beide modellen. Let op dat `target_database_port` al een default heeft; die blijft.
2. **Ontbreken ze, dan zoekt de route de eigen dienst op.** Het project is al bekend, want de API-sleutel hoort bij een project; dat is dezelfde weg die de andere endpoints gebruiken. De verbindingsgegevens van de eigen `postgresql-database` respectievelijk de eigen bucket komen daaruit.
3. **Half ingevuld is een fout, geen gok.** Geef je drie van de vier velden, dan is dat een vergissing en geen verzoek om aan te vullen; weiger dat met een melding die zegt welk veld ontbreekt. Alles of niets.
4. **Vul de lege plek in `plans/vragen-uit-zad-cli.md`** onder vraag 7, met wat er gekozen is en waarom, zodat de CLI het antwoord op dezelfde plek vindt als de rest.

## De vraag die eerst beantwoord moet worden

**Mag een sleutel terugzetten in een dienst van een ánder project?** Vandaag kan dat, want je geeft zelf een host en een wachtwoord op en niets controleert of die bij jou horen. Zodra de velden optioneel worden, is de vraag of dat zo hoort te blijven.

Meet dat eerst, en behandel het als een beveiligingsvraag en niet als een detail: als een sleutel van project A vandaag een backup van A in de database van B kan schrijven, is dat een bestaand gat en niet iets wat dit plan introduceert. Zeg in beide gevallen expliciet wat er gekozen is.

## De toets

- een verzoek **zonder** doelvelden zet terug in de eigen dienst van het project bij de sleutel, en dat is te zien in de taakuitkomst;
- een verzoek **met** alle doelvelden doet exact wat het vandaag doet, dus een bestaande aanroep verandert niet van gedrag;
- een verzoek met een **deel** van de velden faalt met een melding die het ontbrekende veld noemt;
- hetzelfde geldt voor buckets, met dezelfde regels;
- een project zonder database of zonder bucket krijgt een begrijpelijke fout en geen stacktrace;
- `plans/vragen-uit-zad-cli.md` draagt het antwoord onder vraag 7.

## Waar op te letten

**Zet dit niet in een tweede endpoint.** De verleiding is een aparte "restore in place", maar dan bestaan er twee paden die op een haar na hetzelfde doen, en die lopen uit de pas zodra er iets aan verandert. Eén endpoint met optionele velden.

**De bestaande test is het aanknopingspunt.** `tests/test_restore_request_body.py` kwam met RC-77 en gaat precies over dit verzoeklichaam; breid die uit in plaats van er een nieuwe naast te zetten.

**Log niet wat je invult.** Zodra de route de eigen gegevens ophaalt, staan er credentials in het geheugen van die aanroep. Die horen niet in een logregel, ook niet op debugniveau, en ook niet in de foutmelding als de verbinding mislukt.
