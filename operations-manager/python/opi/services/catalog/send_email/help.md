# E-mail versturen

Je applicatie kan e-mail versturen via de mailrelay van het platform. Je krijgt een eigen SMTP-account met een eigen wachtwoord en een eigen dagbudget. De relay stuurt je bericht door naar de mailserver van de organisatie; jouw applicatie hoeft daar zelf niets van te weten.

## Wanneer gebruik je dit?

- Je stuurt gebruikers een bevestiging, een uitnodiging of een wachtwoordlink
- Je stuurt beheerders een melding als er iets misgaat

Dit is alleen voor **uitgaande** e-mail. Berichten ontvangen kan niet: er is geen postbus en er komt geen post binnen op je applicatie.

## Een beheerder moet het eerst goedkeuren

Het aanzetten van deze dienst is een aanvraag. Zolang die niet is goedgekeurd gebeurt er niets.

De stand van je aanvraag staat op de projectpagina bij de deployment, en komt ook terug via de API. Wordt een goedkeuring later ingetrokken, dan wordt het account opgeheven en verdwijnen de gegevens weer uit je deployment.

## Hoeveel berichten per dag

Standaard mag je project **1000 berichten per dag** versturen, en de relay dwingt dat ook echt af. Een lager getal kies je zelf; dat is je eigen grens verlagen en vraagt niemand om toestemming.

Heb je meer nodig, vul dan het getal in dat je nodig hebt. Boven de standaard is dat een aanvraag: een beheerder keurt precies dat getal goed of af, en de stand staat op je projectpagina.

Tot die goedkeuring blijft het getal gelden dat je al had, dus nooit minder. Verhoog je een goedgekeurd getal, dan is dat een nieuwe aanvraag; verlagen gaat direct. Kom je later terug op een getal dat al eens is goedgekeurd, dan geldt die goedkeuring nog, tenzij die waarde daarna is afgewezen. Er is geen vast maximum: 100.000 berichten per dag is een gewone aanvraag.

De grens telt **per bericht en niet per ontvanger**. Een bericht aan 20 ontvangers is er een, niet twintig. Zit je aan je maximum, dan weigert de relay het volgende bericht tijdelijk (`452`) en probeert je mailbibliotheek het later opnieuw.

## Wat wordt er ingesteld?

Na goedkeuring wordt er een SMTP-account voor je project aangemaakt op de relay. Je component krijgt **SMTP_HOST**, **SMTP_PORT**, **SMTP_USERNAME**, **SMTP_PASSWORD** en **SMTP_FROM**. Ook wordt het netwerkverkeer van je component naar de relay opengezet; naar de mailserver van de organisatie hoeft je project zelf geen verbinding te hebben.

Vul je geen eigen afzenderadres in, dan draagt het adres de naam van je project: `noreply-rijksapp+<jouw project>@rijksoverheid.nl`. De relay schrijft die `From:` zelf, met de naam die je hieronder invult ernaast:

    Algoritmeregister <noreply-rijksapp+algor-odc@rijksoverheid.nl>

Zet je applicatie zelf een `From:`, dan hoeft dat geen fout te geven: de relay overschrijft hem. Wat de ontvanger ziet, komt uit de velden hieronder en nergens anders. Vul je niets in, dan verstuur je zonder naam; dat mag.

## Een eigen afzenderadres aanvragen

Wil je iets anders, bijvoorbeeld `noreply@rijksoverheid.nl`, vul dat dan in bij het afzenderadres. Het opslaan is de aanvraag; een beheerder keurt hem goed of af, en de stand staat op je projectpagina. Tot die goedkeuring vertrekt je post onder het adres van je project. Wijzig je een goedgekeurd adres, dan is dat een nieuwe aanvraag, behalve als je terugkomt op een adres dat al eens is goedgekeurd en niet daarna is afgewezen; maak je het veld leeg, dan trek je de aanvraag in.

Het domein ligt vast op `rijksoverheid.nl`: alleen het deel voor de `@` kies je zelf. Mail via de Rijksoverheid-server draagt hun domein; een ander domein faalt DMARC en komt niet aan.

Onbestelbare berichten komen op je projectadres (herleidbaar). De relay zet ook een kopregel `X-ZAD-Project` met je projectnaam.

Je `Reply-To:` blijft van jou. Op het afzenderadres komt geen post, dus zet je `Reply-To:` in je applicatie voor antwoorden. Een `noreply`-`From:` met eigen `Reply-To:` is het normale geval.

Voor de naam gelden een paar grenzen, omdat hij rechtstreeks in de kopregels van het bericht terechtkomt: geen regeleindes, geen `@`, geen punthaken, aanhalingstekens, backslash of dollarteken, en hoogstens 64 tekens. Voor het adres geldt: hoogstens 64 tekens voor de `@`, uit kleine letters, cijfers en `. _ + -`.

De relay zet ook de dingen recht die je applicatie niet hoort te regelen: het adres waarop een onbestelbaar bericht terugkomt, de digitale handtekening op je post, en het weghalen van technische kopregels waar de interne namen van het cluster in staan.
