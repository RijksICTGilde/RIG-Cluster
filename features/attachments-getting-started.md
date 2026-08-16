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

## Een bijlage vervangen

Een certificaat verloopt en je hebt een nieuw bestand. Gebruik dan **Vervangen**, niet verwijderen-en-opnieuw-uploaden: bij vervangen blijft de identifier staan, **en dus blijven alle koppelingen staan**. Elk component dat de bijlage gebruikt, blijft eraan gekoppeld. Verwijderen en opnieuw uploaden verbreekt die koppelingen; je zou ze daarna per component opnieuw moeten leggen.

1. Ga naar de sectie **Bijlagen** en klik bij de bijlage op **Vervangen**.
2. Kies het nieuwe bestand. De identifier ligt vast en is niet te wijzigen.
3. Klik op **Vervangen** en daarna op **Opslaan**.

De naam van het nieuwe bestand wordt overgenomen, zodat wat er in de lijst staat klopt met wat erin zit. De vorige inhoud is daarna weg: er is geen versiehistorie.

Wil je een bijlage onder een *andere* identifier? Dat is geen vervanging maar een nieuwe bijlage; je moet dan ook elke koppeling omzetten.

### Via de API

Vervangen is `PUT` op de bijlage zelf. De id staat in het pad, het nieuwe bestand gaat als multipart mee:

```bash
curl -X PUT -H "X-API-Key: <key>" \
  -F file=@nieuw-certificaat.pem \
  https://<host>/api/v2/projects/<project>/services/attachments/attachment/server-cert
```

`PUT` eist dat de bijlage bestaat en weigert een id die er niet is (404) -- een vervanging van iets wat er niet is, is een vergissing en geen aanmaak. Wil je "aanmaken of overschrijven, wat er ook staat", gebruik dan `?upsert=true`.

## De pod krijgt de nieuwe inhoud ook echt

Een vervangen bijlage moest tot RC-119 wachten tot er toevallig iets anders met je project
gebeurde: de wijziging werd opgeslagen in git en verder gebeurde er niets. En zelfs als er
wel een manifest werd gegenereerd, bleef een draaiende pod het oude bestand houden: een
bijlage wordt met een `subPath` gemount en zo'n bestand werkt Kubernetes principieel nooit
bij. Hetzelfde gold voor je eigen env-vars, die via een secret met een vaste naam
binnenkomen.

Twee dingen lossen dat samen op, en je merkt er verder niets van:

* de pod-template draagt een **hash van de inhoud** van de geheimen die dit component
  leest (je bijlagen en je env-vars). Verandert de inhoud, dan verandert de pod-spec en
  start de pod opnieuw met het nieuwe bestand. Verandert er niets, dan verandert de hash
  niet en herstart er niets. In de annotatie staat alleen de hash, nooit de inhoud;
* de API-routes voor bijlagen **rollen zelf uit**. Ze antwoorden daarom `202` met een
  `task_id` in plaats van `200`/`201`, en er zit een `Location`-header bij waarmee je de
  uitrol kunt volgen. Een weigering (409, 422, 404) krijg je nog steeds meteen.

Wil je meerdere wijzigingen verzamelen en in één keer uitrollen, stuur dan `?rollout=false`
mee — dezelfde betekenis als bij de andere endpoints. Je rolt daarna uit met
`POST /api/v2/projects/{project}/:refresh`.

```bash
curl -X PUT -H "X-API-Key: <key>" \
  -F file=@nieuw-certificaat.pem \
  'https://<host>/api/v2/projects/<project>/services/attachments/attachment/server-cert?rollout=false'
```

Alleen de componenten die deze bijlage of deze env-vars gebruiken herstarten. Een project
met vijf componenten waarvan er één het certificaat leest, herstart die ene.

## Een bijlage verwijderen

Klik in de sectie **Bijlagen** op **Verwijderen**. Wordt de bijlage nog gebruikt door een component of als webcertificaat, dan kan dat niet en zie je waar 'ie in gebruik is. Verwijder eerst die koppeling(en).

### Via de API

Verwijderen kan ook rechtstreeks, op dezelfde plek waar je 'm bijwerkt:

```bash
curl -X DELETE -H 'X-API-Key: <key>' \
  https://<host>/api/v2/projects/<project>/services/attachments/attachment/<id>
```

Ook hier is het antwoord een `202` met een `task_id`: de bijlage is uit het projectbestand
weg zodra je die krijgt, en de taak haalt het secret en de mount van het cluster.

Wordt de bijlage nergens gebruikt, dan is 'ie meteen weg. Wordt 'ie wél gebruikt, dan krijg je een **409** met daarin `used_by`: per plek de componentnaam, de deployment (als de koppeling daar zit) en of het om een koppeling of om een certificaat gaat. Zo weet je wat je op het spel zet voordat je doorzet.

Wil je 'm toch weg hebben, dan bevestig je dat je die lijst gezien hebt:

```bash
curl -X DELETE -H 'X-API-Key: <key>' \
  'https://<host>/api/v2/projects/<project>/services/attachments/attachment/<id>?confirm_in_use=true'
```

De bijlage én alle koppelingen ernaar gaan dan in één keer weg — bij componenten en, waar die bestaan, bij de componenten binnen een deployment. Het antwoord meldt in `uncoupled_from` wat er is losgekoppeld. Houdt een component daarna geen enkele bijlage meer over, dan verdwijnt het lege bijlagenblok en blijft alleen de dienstselectie staan.

De vlag heet `confirm_in_use` en niet `force`: je bevestigt dat je weet dát 'ie in gebruik is, niet alleen dat je iets wilt overrulen. Hij staat standaard uit.

**Eén uitzondering.** Dient de bijlage als eigen webcertificaat (**Webadres** in de modus *eigen certificaat*), dan wordt verwijderen ook mét bevestiging geweigerd. Die verwijzing kun je niet zomaar weghalen: dan zou er wel *eigen certificaat* staan maar geen certificaat zijn. Zet het webadres eerst terug op een andere modus, dan kan de bijlage weg.

## Controleren of het goede bestand er staat

De inhoud komt nergens uit de API terug, en dat blijft zo. Wél terug komt wat je nodig hebt om te controleren dát het goede bestand er staat: `GET /api/v2/projects/{project}/services` geeft de catalogus terug onder `data` van de bijlagen-dienst op projectniveau, per bijlage met zijn `id`, `filename`, `size` in bytes en de `sha256` van de inhoud.

```jsonc
{"name": "attachments", "usages": [
  {"target": "project", "config": null, "data": [
    {"id": "server-cert", "filename": "server.pem", "size": 1834, "sha256": "9f86d0…"}
  ]}
]}
```

Die checksum gaat over de **ontsleutelde** inhoud, dus over het bestand zoals jij het uploadde. Een checksum over de versleutelde vorm zou niets zeggen: AGE versleutelt niet deterministisch, dus hetzelfde bestand levert bij elke upload een ander blok en dus een andere checksum. Vergelijk met `sha256sum server.pem` en je weet of wat er staat is wat je stuurde.

Staat er `"size": null` en `"sha256": null`, dan is de bijlage er wel maar was de opgeslagen inhoud niet te lezen. Dat is iets anders dan een leeg bestand.

## Goed om te weten

- Maximaal 64 KB per bestand.
- Een omgevingsvariabele werkt alleen voor tekst; binaire bestanden koppel je als bestand.
- De inhoud wordt versleuteld opgeslagen en is in het portaal niet terug te lezen.
