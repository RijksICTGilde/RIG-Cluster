# VLAM-API

Deze dienst geeft je applicatie toegang tot de VLAM-API, de taalmodel-API van SSC-ICT, vanuit je eigen pods en zonder VPN.

Je eigen VLAM-sleutel stuur je zelf mee vanuit je applicatie; zet die als eigen omgevingsvariabele (`user-env-vars`), bijvoorbeeld als `VLAM_KEY`. Die sleutel krijg je bij SSC-ICT, niet via ZAD.

## Welke weg je kiest

Elk component van elke deployment krijgt `VLAM_API_URL`: het adres van onze proxy, die de beveiligde verbinding met VLAM **termineert** en zelf het certificaat controleert. Daar hoef je niets voor in te stellen.

En op een cluster dat het **doorlus-pad** aanbiedt daarnaast `VLAM_API_URL_DIRECT`. Dat tweede adres gaat naar dezelfde proxy op een andere poort, maar daar wordt de verbinding **doorgelust**: hij loopt versleuteld door tot aan VLAM, en jij controleert het certificaat in je eigen applicatie.

Staat die variabele niet in je pod, dan biedt dit cluster de doorlus niet aan en is `VLAM_API_URL` de enige weg. Schrijf je applicatie dus niet vast op een variabele die je er niet in ziet staan.

**Kies `VLAM_API_URL`, tenzij je een reden hebt om dat niet te doen.** Het werkt zonder dat je iets instelt, en het stuk verkeer dat onversleuteld is blijft binnen ons eigen cluster en beheer.

**Kies `VLAM_API_URL_DIRECT` als je versleuteling tot aan VLAM zelf nodig hebt** en die eis niet mag eindigen bij onze proxy. Instellen hoef je daarvoor niets: het certificaat van VLAM komt van een publiek vertrouwde uitgever, dus de gewone certificatenlijst van je image is genoeg. Draai je op een image dat helemaal geen CA-certificaten bevat, zoals sommige distroless- of scratch-images, dan lukt dit net zo goed niet als elke andere HTTPS-aanroep uit zo'n image.

## Wat de dienst nog meer doet

- Het netwerkverkeer van je pods naar de proxy wordt opengezet: naar de poort van `VLAM_API_URL`, en op een cluster met het doorlus-pad ook naar die van `VLAM_API_URL_DIRECT`. Zonder de dienst heeft je pod geen weg naar die namespace.
- Voor het doorlus-pad zet de dienst een regel in `/etc/hosts` van je pod, die de VLAM-naam naar onze proxy wijst. Dat moet, want de naam moet in de URL blijven staan: je controleert het certificaat op die naam. Je hoeft daar zelf niets voor te doen.

Verwacht je bibliotheek een andere naam dan `VLAM_API_URL`, gebruik dan een alias op je component.
