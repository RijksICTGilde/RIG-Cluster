# Leeg is niet hetzelfde als onleesbaar

Status: plan, 10 augustus 2026. Antwoord op de melding van zad-cli over `env_var_names: null`, live geverifieerd op de sandbox tegen de endpoints uit RC-61.

## De klacht, en hij klopt

Alle zeven componenten van `hwt-nqi` komen terug met `env_var_names: null`, terwijl geen van die componenten eigen omgevingsvariabelen heeft. In hetzelfde antwoord geeft `aliases` wél `{}` terug, dus daar wordt leeg wel van onbekend onderscheiden.

```json
{"name": "component1", "env_var_names": null, "aliases": {}, "ports": {"inbound": []}}
```

Het contract zegt dat `null` iets anders betekent. Het veld documenteert zichzelf zo:

> Null means the stored variables could not be read, which is not the same as having none.

De CLI volgt dat netjes en toont `null` als `(unreadable)`. Omdat elke component zonder variabelen `null` teruggeeft, leest de hele tabel nu alsof er overal iets mis is. Daarmee is een onderscheid dat met opzet is aangebracht in de praktijk betekenisloos geworden, wat erger is dan het onderscheid niet maken.

## De oorzaak, in één regel

`opi/services/project_env_vars.py:44`:

```python
if not raw:
    return None
```

De docstring van diezelfde functie spreekt zichzelf tegen:

> The parsed variables, or None when **nothing is stored** or the value could not be read. None means "unknown", never "empty".

Twee gevallen, één antwoord, en de tweede zin is degene die met het API-contract klopt.

## De oplossing

Splits de gevallen:

| Situatie in het projectbestand | Antwoord |
|---|---|
| `user-env-vars` ontbreekt, of staat er leeg | `{}` (we hebben gekeken, er zijn er geen) |
| `user-env-vars` staat er, maar ontcijferen of parsen mislukt | `None` (onbekend) |
| `user-env-vars` staat er en is leesbaar | de variabelen |

Alleen het tweede geval is onbekendheid, en alleen daar hoort de waarschuwing bij. De docstring gaat mee: de eerste zin klopt straks niet meer.

De voorstel-tekst van zad-cli is hier leidend en er valt weinig aan toe te voegen. Wat er nog bij hoort is dat `not raw` ook een lege string en een lege mapping vangt: een sleutel die er staat maar niets bevat is "we hebben gekeken en er zijn er geen", dus die valt in de eerste rij en niet in de tweede.

## Let op de tweede lezer

Deze functie is niet alleen van de API. Sinds RC-61 gebruikt de projectdetailpagina hem ook, en dat was juist de bedoeling van die extractie: één ontsleutelpad in plaats van twee die uit elkaar lopen.

```
opi/web/router.py:1363   deployment-componentvariabelen
opi/web/router.py:1373   componentvariabelen
opi/api/v2/project_read.py:255   het leesendpoint
```

Voor de detailpagina verandert er niets zichtbaars: `templates/project-details/section-env-vars.html.j2` toont een blok alleen wanneer de waarde een mapping is met lengte groter dan nul, en een lege mapping valt daar net zo goed buiten als `None`. Maar dat is een aanname die je moet controleren en niet moet geloven, dus dat hoort in de verificatie.

Verifieerbaar per geval:

1. Een component zonder `user-env-vars` levert `env_var_names: []` en niet `null`.
2. Een component met een onleesbaar blok (verkeerde sleutel) levert nog steeds `null`, en er staat één waarschuwing in de log zonder namen of waarden erin.
3. Een component met variabelen levert de namen, gesorteerd, en geen waarden.
4. De projectdetailpagina toont voor en na dezelfde HTML voor alle drie de gevallen.

## Waar op te letten

**Verander alleen dit.** De verleiding is om `aliases` en `attachments` er meteen bij te trekken omdat die "ook wel eens leeg zijn". Die geven al `{}` en `[]` terug en zijn niet stuk.

**Het is een contractwijziging, hoe klein ook.** `env_var_names` gaat van `list | None` naar in de praktijk vrijwel altijd een lijst. De CLI hoeft niets te doen, dat schrijven ze zelf, maar de OpenAPI-omschrijving van het veld moet mee: daar staat nu de zin die de verwarring veroorzaakte.

**Geen waarden in de log.** De bestaande waarschuwing bij een leesfout noemt alleen welk component het betrof. Dat blijft zo; een fout in dit pad mag nooit een naam of waarde lekken. Dat was een uitgangspunt van RC-61 en het verandert hier niet.
