# Na een restore is het project op slot

Een restore meldt `success` en laat het project achter met een databasegeheim dat niet meer werkt. Daarna faalt **elke** volgende wijziging op dat project. Gevonden in de tweede sandboxdoorloop (RC-110), twee keer gereproduceerd op twee projecten.

## Wat er gebeurt

De restore roteert het wachtwoord van de databasegebruiker:

```
01:28:34,261  Password updated for user e2e62_glv_productie
01:28:34,535  Database e2e62_glv_productie_v1 created successfully
```

Daarna start hij de reparatie die de manifesten en het geheim zou moeten bijwerken, en die faalt op precies dat nieuwe wachtwoord:

```
01:28:47,236  Triggering project refresh for e2e62-glv
01:28:48,765  ERROR  Error processing project: Database secret exists for e2e62-glv/productie
              but credentials are invalid. Manual intervention required to fix database user
              or update secret.
01:28:48,770  WARNING  Skipping ArgoCD sync due to critical failures
```

De stand daarna, nagemeten:

| Wat | Stand |
|---|---|
| Antwoord van de API | `{"status":"success", ..., "refresh_triggered": true}` |
| Geheim in de namespace | onveranderd sinds het aanmaken (`resourceVersion` gelijk) |
| `DATABASE_DB` in dat geheim | de **oude**, inmiddels lege database |
| `DATABASE_PASSWORD` | `psql`: `password authentication failed` |
| Nieuwe manifesten in `zad-deployments` | geen |
| ArgoCD | `Synced` / `Healthy` -- hij ziet niets veranderen |

En het blijft niet bij dat geheim: een `POST .../services` erna faalt met dezelfde melding. Het project is op slot tot iemand met de hand ingrijpt.

Het venijnige is dat `success` niet gelogen is. De restore is inhoudelijk geslaagd, het merk staat terug. Wie op dat antwoord afgaat heeft alleen een project dat niet meer werkt en dat ook niet meer bij te werken is.

De hele weg is nieuw ten opzichte van main (`33f6fd0c feat(restore): terugzetten in de eigen database of bucket zonder doelvelden`), dus dit is geen bestaand gedrag.

## De beslissing die eerst genomen moet worden

**Wie mag het geheim bijwerken?** De melding *Manual intervention required* staat er niet voor niets: hij voorkomt dat OPI een geheim overschrijft dat iemand anders beheert. Er zijn twee wegen, en ze zijn niet gelijkwaardig.

**(a) De restore werkt het geheim zelf bij.** Hij heeft het wachtwoord zojuist zelf gezet, dus hij weet als enige wat het nieuwe geheim moet zijn. De bescherming blijft overeind voor alle andere gevallen, want alleen dit ene pad krijgt het recht. Ingrijpender in de code, maar het houdt de regel scherp: wie roteert, werkt bij.

**(b) De refresh accepteert een wachtwoord dat de restore net zelf heeft gezet.** Kleiner, maar het verzwakt de controle die de fout juist ving, en het vraagt om een manier om "net zelf gezet" te herkennen. Dat is een toestand die ergens vandaan moet komen en die kan verlopen of blijven hangen.

**Voorstel: (a).** De restore is de enige die het antwoord kent, en dan hoort hij het ook op te schrijven. Leg de keuze en de reden vast in de PR-beschrijving.

Er is nog een derde weg die geen reparatie is maar wel een geldige uitkomst: **de weg dichtzetten** tot dit klopt. Als (a) te groot blijkt voor deze release, is een restore die weigert beter dan een restore die een project sloopt en `success` zegt.

## Taken

### 1. Een test die het reproduceert

Eerst rood, dan pas repareren. Een restore in de eigen database, en daarna:

- het geheim in de namespace is bijgewerkt (`resourceVersion` veranderd);
- `DATABASE_DB` wijst naar de database die de restore heeft gevuld;
- de opgeslagen inloggegevens werken echt (een verbinding maken, niet alleen de tekst vergelijken);
- **een wijziging na de restore slaagt.** Dit is de assertie die er het meest toe doet: de fout die de gebruiker treft is niet de restore zelf maar alles daarna.

### 2. De reparatie

Volgens de gekozen weg. Let erop dat de manifesten in `zad-deployments` ook echt worden weggeschreven; nu blijft de laatste commit dateren van vóór de restore, en dan verandert er voor ArgoCD niets en meldt hij vrolijk `Healthy`.

### 3. Het antwoord van de API mag niet liegen

Slaagt de restore maar mislukt het bijwerken, dan hoort dat in het antwoord te staan en niet alleen in het log. Nu meldt hij `success` met `refresh_triggered: true` terwijl de refresh is omgevallen. Een aanroeper die niet in de OPI-logs kan kijken -- en dat is elke aanroeper -- heeft geen enkele manier om dit te merken.

### 4. Verifiëren op de sandbox

Restore doen, daarna een dienst toevoegen, en zien dat die slaagt. Dat is de doorloop waar het op stukliep.

## Wat er buiten valt

- Restores naar een extern doel; die roteren geen wachtwoord van ons.
- De bredere vraag of `Manual intervention required` op andere plekken te streng is.
