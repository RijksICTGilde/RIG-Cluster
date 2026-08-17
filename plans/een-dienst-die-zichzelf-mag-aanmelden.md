# Een dienst die zichzelf mag aanmelden

Status: plan, 12 augustus 2026. Aanleiding: via de API kun je een dienst aan een component of deployment hangen, maar in de UI moet je hem eerst op projectniveau aanzetten, anders is hij niet verder te configureren. Voor diensten die zonder keuzes werken, zoals een database of opslag, is die tussenstap alleen maar werk. Voor andere is de projectlaag juist de plek waar iets vastgelegd MOET worden voordat de rest betekenis heeft.

De vraag is dus niet "mag het impliciet", maar **per dienst** of het mag, en zo ja met welke standaardwaarden in het projectbestand. En dat antwoord hoort bij de dienst te staan, niet in een lijstje in de API.

## Wat er nu is, gemeten

Er zijn 23 dienstpakketten in `opi/services/catalog/`. Elf ervan declareren iets op de projectlaag (`ConfigLayer.PROJECT`): attachments, authorization_wall, cross_domain_access, invite, keycloak, minio, namespace_postgres, postgresql_database, publish_on_web, redis, sleep_mode. De rest niet.

Dat "iets declareren" is nog geen antwoord op onze vraag. Een dienst kan projectvelden hebben die allemaal een bruikbare standaard kennen (dan mag hij impliciet), of één veld zonder standaard dat de gebruiker moet kiezen (dan mag hij niet). Dat onderscheid staat nergens vastgelegd.

De haken zitten op `opi/services/catalog/base.py`: `config_model_for(layer)` (703), `config_editables(layer)` (795), `config_approvals(layer)` (983). De registry verzamelt per soort met functies als `provisioning_services()` en `selected_services(project_data)` in `opi/services/registry.py`.

## Wat er moet gebeuren

### 1. Een haak waarin een dienst zegt of hij zichzelf mag aanmelden

Voeg één haak toe op `Service`, in dezelfde vorm als de bestaande: de basisklasse geeft het **veilige** antwoord (nee, expliciet aanzetten) en elke dienst die het anders wil zegt dat zelf. Dan werkt het vanzelf voor de diensten die er nu zijn en voor die er nog komen, en is er geen lijst die achterloopt.

De haak moet twee dingen kunnen zeggen, want ze horen bij elkaar:

* **mag deze dienst impliciet ontstaan** als iemand hem aan een component of deployment hangt;
* **wat komt er dan op de projectlaag te staan**, dus de standaardconfiguratie die het projectbestand geldig houdt.

Dat tweede is essentieel: impliciet toevoegen betekent dat het projectbestand een blok krijgt dat niemand heeft ingevuld, en dat blok moet door de schemavalidatie komen. Een dienst die geen bruikbare standaard kan geven, kan dus per definitie niet impliciet.

Toets de haak op zichzelf: de basisklasse zegt nee, en een dienst die ja zegt zonder standaardconfiguratie te kunnen leveren is een programmeerfout die luid moet falen en niet stil doorgaan.

### 2. Per dienst bepalen wat het antwoord is

Dit is het eigenlijke werk en het is niet af te raffelen. Loop de 23 dienstpakketten langs en leg per dienst vast: mag het impliciet, en zo ja met welke standaard. Schrijf de **reden** erbij, want die is over een half jaar belangrijker dan de keuze.

De gebruiker geeft de richting: database en opslag zijn de duidelijke ja's, want daar volstaan defaults. Diensten waar op de projectlaag eerst iets gekozen moet worden zijn de duidelijke nee's. Publish-on-web is een goed voorbeeld om zorgvuldig te bekijken: het project legt vast op welke domeinen gepubliceerd mag worden, en dat is precies zo'n keuze die niet standaard te verzinnen is.

Twijfelgevallen horen bij nee. Een dienst die per ongeluk impliciet ontstaat met een verzonnen standaard is erger dan een foutmelding die zegt dat je hem eerst moet aanzetten.

### 3. De API gebruikt de haak

Hangt iemand een dienst aan een component of deployment terwijl die niet op projectniveau staat, dan vraagt de code de haak. Mag het, dan komt de dienst met zijn standaardconfiguratie in de projectlijst en gaat de handeling door. Mag het niet, dan een fout die zegt **welke dienst** eerst op projectniveau aangezet moet worden, en niet een schemafout waar de aanroeper niets aan heeft.

Dat toevoegen loopt via het bestaande opslagpad, dus door dezelfde poort als de rest; niet een eigen schrijfweg naast `save_and_commit_project`.

## De vraag die eerst beantwoord moet worden

**Verandert dit ook de UI, of alleen de API?** De aanleiding is de API, en het plan is daar geschreven. Maar zodra de haak bestaat, is dezelfde vraag te stellen in de wizard: waarom moet je daar eerst op projectniveau aanvinken wat vanzelf zou kunnen?

Beantwoord dat expliciet en verander de UI in deze taak **niet**. Als het antwoord "ja, later ook" is, dan is het een vervolg, en dan is de eis aan deze taak dat de haak dat mogelijk maakt zonder verbouwing.

## De toets

- de basisklasse zegt nee, dus een nieuwe dienst is standaard veilig zonder dat iemand eraan denkt;
- een dienst die ja zegt maar geen standaardconfiguratie levert, faalt luid;
- via de API een database aan een component hangen op een project dat de dienst niet had: de dienst staat erna in de projectlijst, met een configuratie die valideert;
- hetzelfde met een dienst die het niet mag: een begrijpelijke fout die de dienstnaam noemt, en het projectbestand is niet aangeraakt;
- het projectbestand valideert na afloop tegen zijn eigen schemaversie;
- er is per dienst vastgelegd wat het antwoord is en waarom.

## Waar op te letten

**Impliciet aanmaken is een schrijfweg.** Er ontstaat een blok in het projectbestand dat niemand heeft ingevuld. Alles wat daarvoor geldt blijft gelden: door de gevalideerde poort, en niet een dienst die zijn eigen stukje bestand schrijft.

**Een dienst met een goedkeuring mag nooit impliciet.** `config_approvals(layer)` bestaat omdat sommige dingen een besluit van een beheerder vragen. Een dienst die zichzelf aanmeldt en daarmee een goedkeuring omzeilt, is een gat. Toets dat expliciet: als een dienst goedkeuringen declareert, is het antwoord nee, ongeacht wat hij verder zegt.

**Niet en passant de projectlaag opruimen.** Dat elf diensten daar iets declareren en twaalf niet, is een gegeven van vandaag en geen opdracht om dat gelijk te trekken.
