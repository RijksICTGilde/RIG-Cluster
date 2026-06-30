# Bijlagen, aan de slag

Met bijlagen koppel je een bestand, bijvoorbeeld een certificaat, keystore of CA-bundle, aan een component van je project. Het bestand wordt versleuteld in je project bewaard en bij het uitrollen als bestand in de pod gezet of meegegeven als omgevingsvariabele.

## Een bijlage uploaden

1. Open je project in het portaal.
2. Ga naar de sectie **Bijlagen** en klik op **Toevoegen**.
3. Geef een **identifier** op (kleine letters, cijfers en streepjes, maximaal 40 tekens). Daarmee verwijs je later naar dit bestand.
4. Kies het bestand (maximaal 256 KB) en klik op **Uploaden**.

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

## Goed om te weten

- Maximaal 256 KB per bestand.
- Een omgevingsvariabele werkt alleen voor tekst; binaire bestanden koppel je als bestand.
- De inhoud wordt versleuteld opgeslagen en is in het portaal niet terug te lezen.
