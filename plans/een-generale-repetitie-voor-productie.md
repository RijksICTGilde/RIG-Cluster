# Een generale repetitie: van projectbestand tot draaiende deployment

Status: plan, 12 augustus 2026. Aanleiding: er is vandaag veel gewijzigd, en het meeste is per stuk getoetst maar niets in samenhang. Voor het naar productie gaat willen we één doorloop die de hele keten aandoet, op de sandbox op de server en niet lokaal.

Dit is bewust een **toets** en geen bouwtaak. Wat eruit komt is een uitkomst en een oordeel, niet een feature. Vind je onderweg iets kapot, repareer het dan niet stilzwijgend: leg vast wat er misging, want dat is de opbrengst.

## Wat er sinds de laatste doorloop is veranderd

Noem dit niet compleet, maar dit is wat het meest aan de keten raakt en dus de aandacht verdient:

* **De hele paginaschil is verbouwd** naar de NLDD-vlakverdeling: `nldd-page` met de kop en voet in slots, `c-sidebar-section` voor de navigatie, een bovengrens van 1920px. Elke pagina erft daarvan.
* **Impliciete dienstselectie** (RC-84): veertien diensten mogen zichzelf op projectniveau aanmelden zodra je ze aan een component of deployment hangt. Dat is een nieuwe schrijfweg naar het projectbestand.
* **Terugzetten zonder doelvelden** (RC-81) en **een bestemmingsfout is 400 met `InvalidTarget`** (RC-82).
* **Het venster bij een samengevoegde refresh** is dichtgezet, inclusief het geval waarin `pending` op 0 stond terwijl er nog iets openstond (RC-82).
* **`rewrite` naast `path`** in de component-API (RC-80), en **Keycloak-config die niet meer stilletjes velden weggooit** (RC-79).
* **Een stap in de voortgang draagt zijn onderwerp** en de regels zijn Nederlands (RC-83).
* **TLS per deployment-component** (RC-78) en **de deploymentpagina opnieuw** (RC-76).

## Begin bij een SCHONE sandbox

Ga er niet van uit dat de projecten van een vorige run nog goed staan. Er is deze week veel
doorheen gegaan, er draaien meer sessies tegen dezelfde omgeving, en een halve toestand geeft
een uitkomst waar je niets aan hebt. Zet de sandbox opnieuw op met `task sandbox:setup`, en
gebruik `task sandbox:sync` voor infrastructuurwijzigingen.

Controleer daarbij expliciet dat de sleutels en instellingen kloppen voordat je begint: de
AGE-sleutel voor de sandbox is een eigen sleutel (`security/sandbox-key.txt`, niet
`security/key.txt`), en de omgeving draait op `*.sandbox.rijksapp.dev`. Klopt dat niet, dan
faalt alles daarna om de verkeerde reden.

## Wat de doorloop moet aandoen

Doe dit op de sandbox op de server. Draai daar **de volledige e2e-suite**, dus zowel de lokale (`-m "e2e and not sandbox"`) als de sandbox-gebonden (`-m "e2e and sandbox"`, met `E2E_BASE_URL` gezet) -- niet de gerichte selectie die tijdens het bouwen volstaat. Dit is de ene keer dat alles moet draaien; dat kost minuten en dat is hier de bedoeling.

De sandbox-suite (`tests/e2e/test_sandbox_flows.py`, `test_sandbox_all_services.py`, `test_sandbox_component_values_api.py`, `test_sandbox_lotc.py`) is de basis; doe met de hand wat die suite niet aandoet.

Twee tests staan al rood en dat is bekend: `test_lotc_paginamarge.py` bewaakt een kolombreedte onder 1400 terwijl de gekozen bovengrens 1440 oplevert. Dat is een getal en geen fout; noem het in het verslag en laat het niet als verrassing terugkomen.

1. **Conversie van bestaande projectbestanden.** Neem projectbestanden zoals ze vandaag in de projects-repo staan, dus niet alleen een vers aangemaakt project. Toets dat ze migreren naar de huidige schemaversie en dáárna valideren. Let op de bekende valkuil: valideer op de GEMIGREERDE gegevens, niet op de rauwe; dat is precies waar dp-bn7 op strandde en wat stil elke reprocess blokkeerde.
2. **Een project aanmaken via de wizard**, met meerdere diensten aan, en het door de hele keten volgen tot de pods draaien.
3. **Hetzelfde via de API**, want die weg is dit jaar uit elkaar gelopen met de UI en is sindsdien bijgetrokken. Inclusief de nieuwe impliciete dienstselectie: hang een database aan een component van een project dat die dienst niet had, en controleer dat hij in de projectlijst komt met een configuratie die valideert.
4. **Een tweede deployment**, want daar zitten de dingen die met één deployment niet opvallen: het onderwerp bij een voortgangsstap, de TLS-override per deployment-component, de deploymenttabel.
5. **Een backup maken en terugzetten**, met en zonder doelvelden, en een bestemming die niet resolvet om de 400 met `InvalidTarget` te zien.
6. **Reprocess van een bestaand project**, want dat is de handeling die het vaakst stil faalde.

## Wat er per stap vastgelegd moet worden

Van elke stap: wat er gedaan is, wat eruit kwam, en of het klopte. Een doorloop zonder verslag is geen toets, want dan is de volgende keer weer alles onbekend.

**Kijk ook naar het scherm.** Er is vandaag een gereedschap bijgekomen dat inlogt en een pagina op beeld zet: `scripts/kijk_sandbox.py <pad>`. Gebruik dat voor de paginas die je onderweg tegenkomt, want de schil is verbouwd en een groene test zegt niet dat een pagina er goed uitziet. Dat is deze week zes keer de dader geweest.

## De toets

- elk projectbestand uit de projects-repo migreert en valideert daarna;
- een project uit de wizard en een project uit de API komen op hetzelfde uit, en het projectbestand valideert;
- een dienst die impliciet is toegevoegd staat in de projectlijst met een geldige configuratie, en een dienst die dat niet mag geeft een begrijpelijke fout;
- een tweede deployment draait, met zijn eigen certificaatinstelling als die gezet is;
- een backup is terug te zetten zonder doelvelden, en een onbereikbare bestemming geeft 400 met `InvalidTarget`;
- reprocess van een bestaand project doet iets zichtbaars en faalt niet stil;
- er is een verslag met per stap de uitkomst, en een oordeel: kan dit naar productie of niet.

## Waar op te letten

**Repareer niet stilzwijgend.** Vind je iets kapot, dan is dat de opbrengst van deze taak. Leg het vast met wat je deed en wat je zag. Een kleine, evidente fout mag je meenemen; iets dat een besluit vraagt niet.

**Niets naar productie in deze taak.** Dit is een repetitie op de sandbox. De uitrol zelf is een besluit dat daarna genomen wordt, met dit verslag in de hand.

**De sandbox is gedeeld.** Er draaien meer sessies tegen dezelfde omgeving. Ruim je testprojecten op, en gebruik namen waaraan te zien is dat ze van deze doorloop zijn.
