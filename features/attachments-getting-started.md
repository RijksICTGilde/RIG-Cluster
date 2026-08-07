# Bijlagen, aan de slag

Met bijlagen koppel je een bestand, bijvoorbeeld een certificaat, keystore of CA-bundle, aan een component van je project. Het bestand wordt versleuteld in je project bewaard en bij het uitrollen als bestand in de pod gezet of meegegeven als omgevingsvariabele.

## Een bijlage uploaden

1. Open je project in het portaal.
2. Ga naar de sectie **Bijlagen** en klik op **Toevoegen**.
3. Geef een **identifier** op (kleine letters, cijfers en streepjes, maximaal 40 tekens). Daarmee verwijs je later naar dit bestand.
4. Kies het bestand (maximaal 64 KB) en klik op **Uploaden**.

Je kunt meerdere bijlagen toevoegen; ze verschijnen in de lijst. Uploaden kan ook tijdens het aanmaken van een project en via **Services beheren**.

## Een bijlage aan een component koppelen

Een geüploade bijlage doet pas iets zodra je 'm aan een component koppelt.

1. Bewerk het component.
2. Open de sectie **Bijlagen**.
3. Kies de **bijlage** (de identifier die je hebt geüpload).
4. Kies hoe het wordt aangeleverd:
   - **Bestand**: het bestand komt op een pad in de pod te staan. Vul het pad in, bijvoorbeeld `/etc/tls/cert.pem`.
   - **Omgevingsvariabele**: de inhoud wordt de waarde van een env-var. Vul de naam in, bijvoorbeeld `CA_BUNDLE`. Dit werkt alleen voor tekstbestanden.
5. Opslaan. Bij de volgende uitrol staat het bestand in de pod.

Dezelfde bijlage kun je aan meerdere componenten koppelen.

## Per deployment afwijken

Wil je in één deployment, bijvoorbeeld een preview-omgeving, een andere koppeling? Stel de koppeling dan in op het component binnen die deployment. Die overschrijft de standaardkoppeling van het component.

## Een eigen certificaat op je webadres

Lever je zelf een certificaat aan om op je webadres te serveren? Upload het als bijlage (een PEM met het certificaat en de sleutel), kies bij **Webadres** de modus **eigen certificaat** en selecteer die bijlage.

## Een bijlage verwijderen

Klik in de sectie **Bijlagen** op **Verwijderen**. Wordt de bijlage nog gebruikt door een component of als webcertificaat, dan kan dat niet en zie je waar 'ie in gebruik is. Verwijder eerst die koppeling(en).

### Via de API

Verwijderen kan ook rechtstreeks, op dezelfde plek waar je 'm bijwerkt:

```bash
curl -X DELETE -H 'X-API-Key: <key>' \
  https://<host>/api/v2/projects/<project>/services/attachments/attachments/<id>
```

Wordt de bijlage nergens gebruikt, dan is 'ie meteen weg. Wordt 'ie wél gebruikt, dan krijg je een **409** met daarin `used_by`: per plek de componentnaam, de deployment (als de koppeling daar zit) en of het om een koppeling of om een certificaat gaat. Zo weet je wat je op het spel zet voordat je doorzet.

Wil je 'm toch weg hebben, dan bevestig je dat je die lijst gezien hebt:

```bash
curl -X DELETE -H 'X-API-Key: <key>' \
  'https://<host>/api/v2/projects/<project>/services/attachments/attachments/<id>?confirm_in_use=true'
```

De bijlage én alle koppelingen ernaar gaan dan in één keer weg — bij componenten en, waar die bestaan, bij de componenten binnen een deployment. Het antwoord meldt in `uncoupled_from` wat er is losgekoppeld. Houdt een component daarna geen enkele bijlage meer over, dan verdwijnt het lege bijlagenblok en blijft alleen de dienstselectie staan.

De vlag heet `confirm_in_use` en niet `force`: je bevestigt dat je weet dát 'ie in gebruik is, niet alleen dat je iets wilt overrulen. Hij staat standaard uit.

**Eén uitzondering.** Dient de bijlage als eigen webcertificaat (**Webadres** in de modus *eigen certificaat*), dan wordt verwijderen ook mét bevestiging geweigerd. Die verwijzing kun je niet zomaar weghalen: dan zou er wel *eigen certificaat* staan maar geen certificaat zijn. Zet het webadres eerst terug op een andere modus, dan kan de bijlage weg.

## Goed om te weten

- Maximaal 64 KB per bestand.
- Een omgevingsvariabele werkt alleen voor tekst; binaire bestanden koppel je als bestand.
- De inhoud wordt versleuteld opgeslagen en is in het portaal niet terug te lezen.
