# Logniveau en redactie van geheimen

Het logniveau van OPI is instelbaar met `LOG_LEVEL`, en een filter op de loghandlers
maskeert bekende geheimvormen in het bericht van elke regel die wordt weggeschreven.

## Waarom

De logstroom van OPI gaat in productie naar Loki. Daarvoor stond de `opi`-logger hard op
DEBUG, dus elke DEBUG-regel van de applicatie ging mee, inclusief het e-mailadres van de
gebruiker bij elk geauthenticeerd verzoek. BIO2 8.15.02 vraagt dat een logregel geen
gegevens bevat die de beveiliging kunnen doorbreken, 8.15.03 bescherming van
loginformatie, en 5.34 dataminimalisatie bij persoonsgegevens.

## LOG_LEVEL

| Omgeving | Waarde |
|---|---|
| Lokaal, sandbox | `DEBUG` (de standaard) |
| Productie (ODCN) | `INFO`, in de configmap van de `odcn-production`-overlay |

De waarde is een niveaunaam van Python (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`)
en bepaalt het niveau van de `opi`-logger. De root-logger blijft op INFO, zodat externe
pakketten hun DEBUG niet lozen; de handlers blijven op DEBUG, zodat `LOG_LEVEL` de enige
grens is. Een onbekende naam valt terug op INFO met een waarschuwing in de log.

`LOG_LEVEL` wordt twee keer gelezen: `opi/core/early_logging.py` leest hem direct uit de
omgeving (logging staat al voordat `config.py` wordt geladen), en `_get_settings` in
`opi/core/config.py` geeft hem daarna door vanuit de settings.

Met `INFO` verdwijnen de regels die per verzoek het e-mailadres noemen
(`opi/middleware/authorization.py`, `opi/core/auth_decorators.py`,
`opi/api/user_token_auth.py`). Toegang van een platformbeheerder tot een project waar hij
geen lid van is, staat juist op INFO: zonder die regel is er in productie geen spoor van
die toegang.

Een lijst die `is_user_authorized_for_project` per project aanroept (het projectoverzicht,
het dashboard, de v2-projectenlijst, de cross-domain-select) geeft `audit=False` mee. Die
regel moet het spoor zijn van een geopend project; zonder die vlag levert elke
overzichtspagina van een platformbeheerder een regel per project op het hele platform, en
verwatert het spoor waar hij voor bedoeld is.

## Het redactiefilter

`SecretRedactionFilter` in `opi/utils/logging_redact.py` hangt op de stdout-handler en op
de bestandshandlers in `setup_logging`. Hij draait dus op elke regel die langs de
root-logger komt, ook op die van een bibliotheek die de redactiehelpers niet kent, en
maskeert in het samengestelde bericht:

| Vorm | Voorbeeld |
|---|---|
| Credential-header met waarde | `X-API-Key: abc123`, `Authorization: Bearer ...`, `Authorization: Basic ...` |
| Bearer-token in vrije tekst | `Got token Bearer eyJ...` |
| `password=waarde` | een URL, een commandoregel, een env-dump |
| `"password": "waarde"` | JSON |
| `PASSWORD 'waarde'` | SQL: `CREATE ROLE`, `ALTER ROLE`, `ALTER USER`, ook `ENCRYPTED PASSWORD` |
| AGE-privésleutel | `AGE-SECRET-KEY-1...` |

De gemaskeerde waarde wordt `***REDACTED***`; de rest van de regel blijft staan, zodat de
logregel zijn vorm houdt. De patronen staan in één lijst (`_SECRET_PATTERNS`) met een
regel commentaar per patroon.

Het filter werkt op het samengestelde bericht (`record.getMessage()`), en ook op de
traceback en de stack die de formatter er bij een `logger.exception` of een
`stack_info=True` achter zetten. Het formatteert de traceback daarvoor zelf en zet hem
gemaskeerd in `record.exc_text`, want de formatter doet dat pas na het filter. Een
exceptie waarvan het bericht een wachtwoord of token bevat, komt dus ook gemaskeerd in de
log.

De patronen zijn opzettelijk nauw gekozen, zodat ze geen gewone regels vervormen. Een
Bearer-token moet minimaal 20 tekens zijn, anders zou "Bearer authentication failed" ook
worden gemaskeerd; de JSON-vorm eist een gequoteerde sleutel, anders zou "Failed to
decrypt registry password: \<fout\>" zijn fouttekst kwijtraken; en `cookie` staat niet in
de namenlijst van het tekstpatroon, omdat dat woord in gewone regels voorkomt ("Invalid
session cookie signature - tampering or SECRET_KEY mismatch").

De access-log van uvicorn komt hier niet langs: `uvicorn.access` heeft zijn eigen handler
met `propagate: false`, dus de filters daarvoor hangen op de logger zelf. Een filter dat
daar het bericht herschrijft kan dat ook niet zonder meer: de `AccessFormatter` van
uvicorn leest `record.args` als een vaste vijftal, en dit filter zet `args` op `None`
zodra het maskeert.

Naast dit filter staat `InviteKeyPathFilter` op de access-logger van uvicorn (RC-313).
Die vervangt de uitnodigingssleutel in een pad door zijn vingerafdruk in plaats van door
`***REDACTED***`, zodat je een reeks verzoeken nog kunt volgen. Dat patroon hoort daarom
niet in `_SECRET_PATTERNS`: dit filter schrijft in het record en hangt op elke handler,
dus een tweede handler zou de vingerafdruk nog eens hashen en de correlatie met de
regels van de routes verbreken.

Drie lagen, in deze volgorde:

1. **Niet loggen.** Een logregel noemt dat een waarde aanwezig is, niet een stuk ervan.
   Een deel van een sleutel of token is nog steeds sleutelmateriaal.
   `tests/test_geen_geheim_in_een_logregel.py` bewaakt dat: een `logger.`-aanroep met een
   slice van een naam met `KEY`, `SECRET`, `TOKEN`, `PASSWORD`, `SQL` of `STATEMENT` laat
   de test falen. Een SQL-statement hoort als soort plus doel in de log, zoals
   `_summarize_sql_statement` in `opi/connectors/postgres.py` het samenvat
   (`CREATE ROLE app_user`), niet als tekst waarin het wachtwoord staat.
2. **`redact_sensitive_headers(headers)`** voor een header-dict die je wilt loggen,
   **`redact_text(tekst)`** voor tekst die niet via een logregel gaat, en
   **`redact_structure(dict)`** voor een dict met zulke teksten erin. De fouttekst van een
   async taak loopt hierlangs, want die gaat naar de database en naar de client. Het
   antwoord van `GET /tasks/{id}` draagt diezelfde tekst in `error_message` en in
   `result` (beide geredigeerd in `fail_task`, `async_task_service.py`) en in de fout per
   subtask (geredigeerd in `PersistentTaskProgressManager._flush_to_db`). `current_step`
   draagt sinds RC-282 geen fouttekst meer, maar loopt in `_flush_to_db` wel langs
   dezelfde redactie, omdat `update_current_step` het veld met vrije tekst vult. De
   redactie zit in `_flush_to_db` en niet in `fail_task`, `fail_subtask` of
   `fail_project`: dat is het enige punt waar de velden de manager verlaten, dus een
   volgende schrijver kan de redactie niet opnieuw vergeten.
3. **Het filter**, als vangnet voor wat de eerste twee missen.

## Configuratie

| Instelling | Standaard | Betekenis |
|---|---|---|
| `LOG_LEVEL` | `DEBUG` | Niveau van de `opi`-logger |
| `LOG_TO_FILE` | `false` | Ook naar een bestand loggen |
| `LOG_FILE_PATH` | `log.txt` | Pad van dat bestand |
| `LOG_ERRORS_TO_FILE` | `false` | WARNING en hoger naar een apart bestand |
| `LOG_ERRORS_FILE_PATH` | `/data/logs/errors.log` | Pad van dat foutbestand |

## Afhankelijkheden

Geen. Het filter gebruikt alleen de standaardbibliotheek en de bestaande
`_SENSITIVE_HEADERS` uit `opi/utils/logging_redact.py`.

## Zie ook

- `features/log-watcher.md` voor de triage van de productielog en het meldkanaal.
