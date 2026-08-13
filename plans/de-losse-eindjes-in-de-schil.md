# De losse eindjes in de schil

Status: plan, 13 augustus 2026. Zeven punten uit een ronde langs het portaal. Ze staan los van elkaar en zijn allemaal klein; ze zitten in één taak omdat ze dezelfde vorm hebben: iets dat er is maar niet doet wat het belooft, of iets dat er hoort te zijn en ontbreekt.

Doe ze los, met een commit per punt.

## 1. Bij een keuzelijst hoort "Optioneel" niet

Op elke wizardstap met een keuzelijst staat "Optioneel" naast het label, ook waar je vrijwel altijd iets moet kiezen. Bij een tekstveld zegt dat label iets (je mag het leeg laten); bij een keuzelijst staat er al een waarde geselecteerd, dus het belooft een vrijheid die er niet is.

Het bestaande `data-no-optional-badge` lost dit niet op, en dat is gemeten: `opi/templates_lotc/components/_forms.j2:104` toetst of dat attribuut in `besturing` zit, maar bij een keuzelijst belandt het op de **omhulling** en niet op het besturingselement. De toets ziet hem daar dus nooit.

De weg is een voorwaarde op `kind` in `nldd_field`; `select-field` is de naam die die macro binnenkrijgt (zie `lotc_forms/templates/components/select-field.html.j2:14`).

**Let op de bewaker.** `opi/templates_lotc/components/_forms.j2` is ONZE kopie van het bestand van lotc-forms, en die kopie ligt op de searchpath vóór de design systems. `tests/test_lotc_foutmelding_veld.py` bewaakt dat hij alleen op vastgelegde punten afwijkt, via de lijst `VERVANGINGEN`. Een derde afwijking moet daar exact bij, anders is die test rood, en dan mist de volgende bouwer een verbetering van upstream.

## 2. De iconen van de diensten staan niet op de dienstkaarten

Op `/projects/services/<naam>` toont elke kaart de naam, het bindingslabel en de omschrijving. Er staat **geen icoon**, terwijl elke dienst er een declareert (`service_def.icon`). Dat is dus geen icoon dat stukging maar een icoon dat nooit gerenderd wordt: `bg/project-tabs.html.j2` rond regel 600 gebruikt `service_def.icon` nergens.

## 3. Het icoon van de bijlagendienst

`opi/services/catalog/attachments/__init__.py:42` declareert `icon="map"`, en de vertaaltabel in `navigation_lotc.py:68` maakt daar `"folder-stack"` van. Of NLDD dat icoon kent is niet vast te stellen: hun bundel is opgesplitst in losse bestanden en de icoonnamen zijn er niet uit te lezen.

Dit is de tweede keer dat een icoon hierop strandt. **Vraag het LOTC-project om de lijst met beschikbare icoonnamen** in plaats van opnieuw te zoeken, en toets daarna in één keer alle namen die wij gebruiken. `to_nldd_icon()` laat een onbekende naam door met een waarschuwing, dus zo'n toets is goedkoop.

## 4. Het icoon bij "Docker image van je applicatie"

Staat niet uitgelijnd met zijn tekst en is veel te groot in verhouding. Meet eerst of dit hetzelfde patroon is als de achttien plekken die eerder zijn gerepareerd: `<c-paragraph>` is `nldd-rich-text` en eist de volle breedte op, dus in een `<c-cluster>` (die wrapt) valt het icoon op een eigen regel. Is dat het, dan is `<c-span>` de oplossing en niet een maat op het icoon.

## 5. De domeinenstap is slecht leesbaar

Op `/forms/wizard/create-project/step/domains` worden teksten kort afgekapt, staat er weinig horizontale ruimte tussen, zweeft het informatie-icoon los van "Hoe worden webadressen gegenereerd?" terwijl het bij "Gegenereerde URL's" juist tegen de tekst plakt.

Dat laatste is waarschijnlijk **hetzelfde als punt 4**, en dan zijn het twee klachten met één oorzaak. Meet dat voordat je twee losse reparaties doet.

## 6. De Keycloak-optie `verify` doet niets

`AccountLink` kent `automatic`, `confirm` en `verify`, maar `opi/manager/keycloak_manager.py:83` zegt het zelf: `None/verify -> stock`. Wie `verify` kiest krijgt dus de standaardflow van Keycloak, precies hetzelfde als niets kiezen. De gebruiker krijgt een derde keuze aangeboden die geen effect heeft.

Weg uit de enum (`config_model.py:35`), uit het schema (`keycloak.v1.0.json`) en uit de keuzelijst. **Beslis wat er gebeurt met projectbestanden die hem al dragen**: een waarde die niet meer valideert blokkeert elke volgende verwerking van dat project, en dat faalt stil. Dat is het gevaarlijke deel van dit punt.

## 7. "Queued" is nog Engels

De UI-teksten zijn naar het Nederlands omgezet, maar `Queued` komt nog voorbij. Het staat als kolomstandaard in de database (`opi/core/async_task_schema.py:18` en `opi/services/persistence/async_tasks.py:35`), dus het is niet alleen een label maar een opgeslagen waarde.

**Vertaal niet blind de opgeslagen waarde.** Bestaande rijen dragen `Queued`, en er kan code op die tekst vergelijken. Vertaal bij de weergave, of vertaal de waarde én de lezers én de bestaande rijen; kies bewust en zeg welke.

## De toets

- geen "Optioneel" meer bij een keuzelijst, en `test_lotc_foutmelding_veld.py` is groen omdat de nieuwe afwijking geregistreerd is;
- de dienstkaarten tonen het icoon van hun dienst;
- elk icoon dat wij gebruiken bestaat in NLDD, getoetst tegen hun lijst;
- het icoon bij "Docker image" en op de domeinenstap staat naast zijn tekst;
- `verify` bestaat niet meer, en een projectbestand dat hem droeg verwerkt nog steeds;
- er staat geen `Queued` meer op het scherm, en bestaande taken blijven leesbaar.

## Waar op te letten

**Kijk naar het scherm.** `scripts/kijk_sandbox.py <pad>` logt in en zet een pagina op beeld. Zes van deze punten zijn met een groene test niet te zien.

**Punt 4 en 5 zijn mogelijk één oorzaak.** Meet dat eerst; twee losse reparaties op hetzelfde patroon lopen daarna uit de pas.
