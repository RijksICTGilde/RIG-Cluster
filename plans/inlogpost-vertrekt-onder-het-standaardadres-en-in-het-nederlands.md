# Inlogpost vertrekt onder het standaardadres, en in het Nederlands

Twee kleine dingen aan de keten die sinds 28 augustus op productie draait, allebei zichtbaar voor de ontvanger van een bevestigingsmail.

Op 28 augustus is de eerste echte bevestigingsmail vanaf `keycloak.rijksapp.nl` bezorgd (`250 ok: Message 132511786 accepted`, queueId 326399568239267840). Daarmee is de keten bewezen. Wat de ontvanger toen kreeg was Engelse standaardtekst van Keycloak, en een afzenderadres dat niet het adres is dat we overal willen. Dit plan repareert die twee.

## 1. Het adres wordt het kale standaardadres

Inlogpost vertrekt nu als `noreply-inloggen@rijksoverheid.nl`. Dat moet `noreply-rijksapp@rijksoverheid.nl` worden: het basisadres dat we overal willen, zonder eigen lokaal deel en zonder plusdeel.

**Het adres staat op DRIE plekken die moeten kloppen**, en de docstring van `get_keycloak_mail_from_address` waarschuwt daar zelf voor: drift laat zich zien als post die onder het ene adres vertrekt terwijl een realm het andere claimt. Alle drie moeten mee:

1. `MAIL_KEYCLOAK_FROM_LOCAL` in `opi/core/config.py`, dat via `generate_keycloak_sender_address` (`opi/utils/naming.py:773`) de afzender van het relayaccount bepaalt.
2. Datzelfde adres dat OPI in de minimale `smtpServer.from` van elke realm schrijft.
3. `ZAD_MAIL_RELAY_FROM`, als letterlijke waarde in `infrastructure/bootstrap/infrastructure/keycloak/controller/base/deployment.yaml:148`.

Laat de aparte afleiding VERVALLEN in plaats van er een andere waarde in te zetten. `generate_keycloak_sender_address` bestaat alleen om een eigen lokaal deel voor te schuiven; zonder dat lokale deel is het het basisadres en is de functie overbodig. Ruim hem op in plaats van hem met een lege waarde te laten staan, en zet in de plaats daarvan een toets die pint dat die drie plekken hetzelfde adres noemen.

**Dit repareert ook `scripts/mail_identity_check.py`.** Dat script toetst dat `From:` en envelope hetzelfde adres dragen. De envelope draagt vandaag al het kale adres (de relay leidt die af uit de accountnaam, en `zad-keycloak` heeft geen `project-`-voorvoegsel), terwijl de `From:` het eigen lokale deel droeg. RC-159 heeft daar een uitzondering voor gemaakt; die kan er nu uit, want de twee vallen weer samen.

**Wat we opgeven, bewust**: `zad-platform` en `zad-keycloak` versturen hierna onder hetzelfde adres en verschillen alleen in weergavenaam. Een bounce is daarmee niet meer te herleiden naar inlogpost of portalpost. Dat is vandaag theoretisch, want er is nog geen bounce-postbus, en het is de prijs voor één herkenbaar afzenderadres.

## 2. De weergavenaam blijft `Rijksapps`

Geen wijziging, wel een besluit dat opgeschreven hoort te worden, want het is drie keer opnieuw gesteld.

`MAIL_KEYCLOAK_FROM_NAME` staat op `Rijksapps` en dat blijft zo. De naam in de `From:` beantwoordt de vraag VAN WIE een bericht komt; het onderwerp beantwoordt waar het over gaat. `Rijksapps` sluit aan bij het adres en bij wat de ontvanger ziet op de plek waar hij inlogt.

Afgewezen alternatieven, met de reden: `Keycloak` is onze productnaam, zegt een ontvanger niets en lekt onnodig welke techniek eronder ligt. `Toegangsbeheer` beschrijft wat wij doen in beheerdersjargon, niet wat de ontvanger herkent.

En de beperking die elke naamkeuze stuurt: dit is EEN account voor alle realms, dus de naam kan nooit een project noemen. Zou dat ooit moeten, dan eindigt daarmee de eenaccountopzet.

## 3. De teksten worden Nederlands

`internationalizationEnabled` staat nergens aan; de drie velden komen in de hele codebase niet voor. Daarom komt er Engelse standaardtekst uit, ongeacht welk thema er staat.

In de blauwdrukken erbij, op de realms die post versturen:

```yaml
internationalizationEnabled: true
supportedLocales: ["nl", "en"]
defaultLocale: "nl"
```

De documentatie van Keycloak stelt daarbij één voorwaarde die makkelijk over het hoofd wordt gezien: een taal is pas beschikbaar als het login-, account- EN emailthema die taal ondersteunen. Voor `nl` is dat het geval; Keycloak levert `messages_nl.properties` mee in het base/email-thema.

Deze drie velden moeten worden toegevoegd aan de lijst die `KeycloakYamlHandler` uit de blauwdruk leest, op de createweg en op de replayweg. Dat mechanisme is sinds RC-159 gezaghebbend, dus dit is precies de beweging waarvoor het gemaakt is. De bestaande toets die met een glob over `opi/configs/keycloak/*.yaml` pint dat elke blauwdruk de gelezen velden noemt, moet mee uitgebreid worden, anders belooft een nieuwe blauwdruk straks weer stil iets wat niet gebeurt.

**`emailTheme` blijft leeg.** Het MinBZK-thema levert geen bruikbaar mailthema (waargenomen: kale Engelse tekst met dat thema geladen), en eigen sjablonen zijn een aparte taak. Zie hieronder.

## Valkuilen

**Het adres staat op drie plekken.** Beweegt er één niet mee, dan vertrekt post onder een ander adres dan een realm claimt. Dat is precies wat de bestaande docstring als faalvorm beschrijft, en het is de reden dat er een toets op moet in plaats van zorgvuldigheid.

**`defaultLocale: nl` raakt meer dan de post.** Het inlogscherm van die realms wordt er ook Nederlands van. Dat is waarschijnlijk gewenst, maar het is een zichtbare wijziging voor bestaande gebruikers en hoort geen verrassing te zijn.

**De Nederlandse vertaling van Keycloak is onvolledig.** Op het moment van schrijven 406 regels tegen 534 Engelse, dus een enkele zin kan in het Engels terugvallen. Dat is geen defect van ons en geen reden om de wijziging tegen te houden; noteer het in de tekst.

**Wanneer het landt.** De relay krijgt de nieuwe afzender wanneer OPI zijn afzendertabel wegschrijft, dus bij een start. Een realm krijgt de nieuwe velden bij zijn eerstvolgende verwerking. De twee lopen dus niet gelijk op, en in dat venster kan een realm een `smtpServer.from` claimen die de relay nog niet kent. Dat is onschuldig (de relay bepaalt de `From:` zelf), maar het hoort opgeschreven te staan.

## Wat hier buiten valt

- **Eigen mailsjablonen en tweetaligheid.** Dat is een eigen taak met een echte ontwerpvraag eronder: wil je EEN taal per bericht op basis van de locale, dan is dit plan de goede weg. Wil je beide talen in EEN bericht, dan werkt het locale-mechanisme juist tegen je (Keycloak rendert precies één locale) en schrijf je eigen FreeMarker-sjablonen. Dat is geen Java en geen build, en de bezorging bestaat al: een ConfigMap gemount onder `/opt/keycloak/themes/`, dezelfde weg als de verzender-jar. Beslis die vork in die taak, niet hier.
- **Een bounce-postbus.** Open punt in `plans/mail-vervolgpunten.md`, en het is wat de attributie die we hier opgeven ooit weer betekenis zou geven.
- **De MTA-STS-opzoeking.** Gemeten op productie: 130 seconden bezorging waarvan 23 milliseconden de bezorging zelf. Eigen punt, ook in `mail-vervolgpunten.md`.

## Verifieerbaar

- Een `execute-actions-email` op een projectrealm levert een bevestigingsmail in het NEDERLANDS.
- De kopregels van dat bericht tonen `Rijksapps <noreply-rijksapp@rijksoverheid.nl>`, en de envelope draagt hetzelfde adres. Zet de kopregels in de PR.
- `scripts/mail_identity_check.py` slaagt, zonder de uitzondering die RC-159 voor dit account maakte.
- Een toets pint dat de drie plekken uit stap 1 hetzelfde adres noemen, en die toets valt aantoonbaar om als je er één losknipt.
- Een blauwdruk die de drie taalvelden weglaat laat de bestaande glob-toets omvallen.
- Een bestaande realm heeft na verwerking `internationalizationEnabled`, `supportedLocales` en `defaultLocale` gezet, en verder geen veld veranderd dat er niet in staat.
- `uv run pytest tests/ -q` groen, plus `ruff check .`, `ruff format .` en `pyright`.
