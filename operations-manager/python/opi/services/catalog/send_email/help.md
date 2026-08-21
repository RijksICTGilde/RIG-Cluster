# E-mail versturen

Je applicatie kan e-mail versturen via de mailrelay van het platform. Je krijgt een eigen SMTP-account met een eigen wachtwoord en een eigen dagbudget. De relay stuurt je bericht door naar de mailserver van de organisatie; jouw applicatie hoeft daar zelf niets van te weten.

## Wanneer gebruik je dit?

- Je stuurt gebruikers een bevestiging, een uitnodiging of een wachtwoordlink
- Je stuurt beheerders een melding als er iets misgaat

Dit is alleen voor **uitgaande** e-mail. Berichten ontvangen kan niet: er is geen postbus en er komt geen post binnen op je applicatie.

## Een beheerder moet het eerst goedkeuren

Het aanzetten van deze dienst is een aanvraag. Zolang die niet is goedgekeurd gebeurt er niets.

De stand van je aanvraag staat op de projectpagina bij de deployment, en komt ook terug via de API. Wordt een goedkeuring later ingetrokken, dan wordt het account opgeheven en verdwijnen de gegevens weer uit je deployment.

## Wat wordt er ingesteld?

Na goedkeuring wordt er een SMTP-account voor je project aangemaakt op de relay. Je component krijgt **SMTP_HOST**, **SMTP_PORT**, **SMTP_USERNAME**, **SMTP_PASSWORD** en **SMTP_FROM**. Ook wordt het netwerkverkeer van je component naar de relay opengezet; naar de mailserver van de organisatie hoeft je project zelf geen verbinding te hebben.

Het afzenderadres ligt vast en draagt de naam van je project: `noreply-rijksapp+<jouw project>@rijksoverheid.nl`. De relay schrijft die `From:` zelf, met de naam die je hieronder invult ernaast:

    Algoritmeregister <noreply-rijksapp+algor-odc@rijksoverheid.nl>

Zet je applicatie zelf een `From:`, dan hoeft dat geen fout te geven: de relay gooit hem weg en zet de zijne ervoor in de plaats - adres en naam allebei. Wat de ontvanger ziet, komt dus uit het veld hieronder en nergens anders. Vul je niets in, dan verstuur je met een kaal projectadres en zonder naam; dat mag.

Waarom je het adres niet zelf kiest: de mail gaat de deur uit via de mailserver van de Rijksoverheid, en die deelt de afzenderidentiteit. Een ander afzenderdomein haalt de controles van de ontvangende mailserver niet en komt eenvoudigweg niet aan.

Je `Reply-To:` blijft wel van jou en wordt niet aangeraakt. Wil je dat een antwoord bij je eigen postbus terechtkomt, zet die dan in je applicatie. Het antwoord komt niet terug op het afzenderadres.

Voor de naam gelden een paar grenzen, omdat hij rechtstreeks in de kopregels van het bericht terechtkomt: geen regeleindes, geen `@`, geen punthaken, aanhalingstekens, backslash of dollarteken, en hoogstens 64 tekens.

De relay zet ook de dingen recht die je applicatie niet hoort te regelen: het adres waarop een onbestelbaar bericht terugkomt, de digitale handtekening op je post, en het weghalen van technische kopregels waar de interne namen van het cluster in staan.
