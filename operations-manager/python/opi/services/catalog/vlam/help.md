# VLAM-API

VLAM is de taalmodel-API van SSC-ICT. Die is niet vanaf internet bereikbaar, maar wel vanaf dit cluster, via een aparte netwerkkoppeling. Met deze dienst kan je applicatie er rechtstreeks bij, zonder VPN en zonder dat je zelf een certificaat hoeft te vertrouwen.

Je component krijgt een adres in een omgevingsvariabele en praat daar gewoon over HTTP mee. Een proxy binnen het cluster zet de beveiligde verbinding naar VLAM voor je op en controleert daarbij het certificaat van VLAM.

## Wanneer gebruik je dit?

- Je applicatie roept een taalmodel aan en draait op dit platform
- Je gebruikt nu de VPN-tunnel om VLAM te bereiken, maar je code draait in het cluster en niet op een laptop
- Je liep vast omdat het certificaat van VLAM niet in de standaardlijst van je programmeertaal zit

Werk je vanaf je eigen machine, dan is dit niet wat je zoekt: daarvoor is de VPN-tunnel. Deze dienst is er voor applicaties die in het cluster draaien.

## Toegang

De dienst aanzetten is genoeg. Aan de kant van VLAM staat één regel die de proxy voor het hele cluster bereikbaar maakt op die ene poort, dus er hoeft niemand per project iets bij te zetten.

Wat je wel zelf regelt is de authenticatie bij VLAM: dat is de API-sleutel die je bij het VLAM-team aanvraagt en die je applicatie meestuurt. De netwerkregel bepaalt alleen of je erbij kunt, niet wat je mag.

## Wat wordt er ingesteld?

Elk component van dit project krijgt **VLAM_API_URL**: het adres van de VLAM-proxy binnen het cluster. Zet dat adres in je eigen configuratie, of gebruik een alias als je bibliotheek een andere naam verwacht. Er komt geen sleutel of wachtwoord bij; wat VLAM zelf aan authenticatie vraagt, regel je in je applicatie.

Daarnaast wordt het netwerkverkeer van je pods naar die proxy opengezet. Verder verandert er niets aan je deployment.

Zet je de dienst weer uit, dan verdwijnen de variabele en de netwerkregel bij de volgende uitrol.

## Waar je op moet letten

Tussen jouw pod en de proxy is het verkeer niet versleuteld. Dat blijft binnen het cluster en binnen ons eigen beheer; de stap naar buiten, van de proxy naar VLAM, is dat wel, en daar wordt ook het certificaat gecontroleerd. Wil je versleuteling tot aan VLAM zelf, dan is de VPN-tunnel het pad dat dat biedt.

Deze dienst bestaat alleen op het cluster dat de koppeling met SSC-ICT heeft. Op andere clusters kan je hem niet kiezen.
