# E-mail versturen

Je applicatie kan e-mail versturen via de mailrelay van het platform. Je krijgt een eigen SMTP-account met een eigen wachtwoord en een eigen dagbudget. De relay stuurt je bericht door naar de mailserver van de organisatie; jouw applicatie hoeft daar zelf niets van te weten.

## Wanneer gebruik je dit?

- Je stuurt gebruikers een bevestiging, een uitnodiging of een wachtwoordlink
- Je stuurt beheerders een melding als er iets misgaat
- Je applicatie heeft een SMTP-instelling die nu op niets uitkomt
- Je wilt niet dat het wachtwoord van de centrale mailserver in je eigen applicatie staat

Dit is alleen voor **uitgaande** e-mail. Berichten ontvangen kan niet: er is geen postbus en er komt geen post binnen op je applicatie.

## Een beheerder moet het eerst goedkeuren

Het aanzetten van deze dienst is een aanvraag. Zolang die niet is goedgekeurd gebeurt er niets: er is geen SMTP-account, je component krijgt geen SMTP-variabelen en er is geen netwerktoegang naar de relay. Wordt de aanvraag afgewezen, dan blijft dat zo.

Dat is expres alles-of-niets. Een account dat wel bestaat maar niet mag versturen is een toestand die niemand kan uitleggen, en die bij het opruimen wordt vergeten.

De stand van je aanvraag staat op de projectpagina bij de deployment, en komt ook terug via de API. Wordt een goedkeuring later ingetrokken, dan wordt het account opgeheven en verdwijnen de gegevens weer uit je deployment.

## Wat wordt er ingesteld?

Na goedkeuring wordt er een SMTP-account voor je project aangemaakt op de relay. Je component krijgt **SMTP_HOST**, **SMTP_PORT**, **SMTP_USERNAME**, **SMTP_PASSWORD** en **SMTP_FROM**. Ook wordt het netwerkverkeer van je component naar de relay opengezet; naar de mailserver van de organisatie hoeft je project zelf geen verbinding te hebben.

Het afzenderadres ligt vast op het maildomein van het platform. Je kiest zelf de naam die de ontvanger ziet en het stuk voor de @; het stuk erachter niet. Dat is geen betutteling maar techniek: een afzender in een vreemd domein haalt de controles van de ontvangende mailserver niet, dus die post komt eenvoudigweg niet aan. Wil je toch versturen vanaf een domein van je project zelf, vraag dat dan aan: daar hoort één DNS-record in de zone van dat domein bij.

De relay zet ook de dingen recht die je applicatie niet hoort te regelen: het adres waarop een onbestelbaar bericht terugkomt, de digitale handtekening op je post, en het weghalen van technische kopregels waar de interne namen van het cluster in staan.

Bij het verwijderen van de dienst wordt het account meteen opgeheven, zodra geen enkele deployment van je project het nog gebruikt.
