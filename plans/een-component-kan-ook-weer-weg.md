# Een component kan ook weer weg

Status: plan, 11 augustus 2026. Klein en afgebakend. Aanleiding: het zad-cli-project kan een component niet verwijderen via de API, en meldt dat zo:

```
Error: This platform has no way to delete a component, so 'web' was left alone.
  why: The API offers only PATCH on /v2/projects/{project}/components/{component}.
```

Dat klopt.

## Wat er nu is, gemeten

| | |
|---|---|
| `/v2/projects/{project}/components/{component}` | alleen **PATCH** |
| dezelfde route in v1 | alleen **PATCH** |
| `TaskType.DELETE_COMPONENT` | bestaat |
| `handle_delete_component` | bestaat en is geregistreerd in `server.py` |

De machinerie ligt er dus volledig; alleen de route ontbreekt. Ergens is de afhandelaar gebouwd zonder dat er ooit een deur naartoe kwam, en dat is van buiten niet te zien.

## Wat er moet gebeuren

Een `DELETE` op datzelfde pad, die de bestaande afhandelaar aanroept. Dezelfde vorm als de andere v2-routes: `@validate_api_token`, een taak, en `rollout` als query-parameter.

**Wat er gebeurt met een component dat nog in gebruik is, hoort een bewuste keuze te zijn.** Een component kan door meerdere deployments gerefereerd worden. Drie mogelijkheden, en dit is precies de afweging die bij het verwijderen van een bijlage (RC-52) al gemaakt is:

1. weigeren met een 409 die zegt waar hij nog gebruikt wordt;
2. verwijderen en de verwijzingen mee opruimen;
3. weigeren, tenzij er een bevestiging meekomt.

RC-52 koos daar 3, met `confirm_in_use`. Volg dat, tenzij er een reden is om het hier anders te doen; twee vergelijkbare handelingen met een verschillende vorm is verwarrender dan één regel die overal geldt. Kijk in `opi/services/catalog/attachments/api.py` hoe dat daar is opgeschreven.

**Kijk wat de afhandelaar al doet** voordat je gedrag toevoegt. Misschien ruimt `handle_delete_component` de verwijzingen al op, en dan is de vraag alleen nog of dat mag zonder te vragen.

## De toets

- een component dat nergens gerefereerd wordt: weg, projectbestand valideert nog;
- een component dat wél in gebruik is: het afgesproken gedrag, en na afloop staat er nergens een verwijzing naar een component dat niet meer bestaat;
- een component dat niet bestaat: een eerlijk antwoord, niet stil succes (zie bevinding 6 van het CLI-project over `DELETE` van een niet-bestaande deployment).

## Waar op te letten

**Dit is een gat in de API, geen nieuw vermogen.** De afhandelaar bestaat, het portaal kan het al. Wat ontbreekt is de weg ernaartoe, en de omvang hoort daarbij te passen.

**De foutmelding van de CLI is het waard om te lezen.** Zij zeggen niet "405" maar wat er wél kan en wanneer het gaat werken. Als deze route er is, hoort hun melding te verdwijnen; laat het ze weten.

**Zoek of er meer van dit soort gaten zijn.** Een geregistreerde `TaskType` zonder route is van buiten onzichtbaar, en dit is er één. Een lijstje van alle `TaskType`-waarden naast de routes die ze aanroepen is zo gemaakt en zegt meteen of er meer zijn.
