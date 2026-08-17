# Keycloak-rechten overdragen in plaats van alles zelf kunnen

Status: ontwerpnotitie, 4 augustus 2026. Niet gebouwd. Aanleiding is een gesprek over de vraag wat de OPI-service-account eigenlijk oplost.

## Waar het om begon

RC-16 gaf OPI een eigen service-account in Keycloak, zodat het menselijke `admin`-account op OTP kan zonder de automatisering buiten te sluiten. Terechte vraag daarbij: als die client vervolgens de volledige `admin`-rol op de master-realm heeft, wat schiet je er dan mee op? Wie die secret bemachtigt kan alsnog alles, zonder tweede factor.

Dat klopt, en het is geen detail. Gemeten in `ensure_master_service_account_client`: de service-account krijgt `admin` op master, oftewel multi-realm admin over heel Keycloak. En gemeten in `keycloak_manager.py`: OPI gebruikt die almachtige identiteit ook voor werk dat maar één realm raakt, terwijl er per project al een realm-scoped account in het projectbestand staat dat het nooit gebruikt.

Twee bezwaren die eerder in dat gesprek zijn weerlegd en die hier niet meer terugkomen:

- *"OTP toevoegen aan de client lost het op."* Nee. OTP hangt aan de browser-flow, maar zelfs als het kon zou de tweede factor náást de eerste in dezelfde pod staan. Voor een **mens** is OTP wel degelijk wezenlijk anders dan een tweede wachtwoord, want de lekwegen verschillen (browsers, hergebruik, phishing, logs); voor een machine met beide geheimen op één plek niet.
- *"Rollen inperken lost het op."* Niet op zichzelf: de client moet nu eenmaal bij alle projectrealms kunnen, want dat is zijn werk.

## Het model: klaarzetten en loslaten

De eigenaar van een realm is het project, niet het platform. Het platform zet klaar en draagt over; daarna is de eigenaar verantwoordelijk, inclusief het verwijderen. Dat betekent drie identiteiten in plaats van één.

| Identiteit | Waar | Mag | Mag niet |
|---|---|---|---|
| Aanmaker | master | `create-realm`: nieuwe realms maken en inrichten, daarna zijn eigen rechten op die realm loslaten | bestaande realms lezen, wijzigen of verwijderen |
| Per-realm client | in de projectrealm zelf | alles binnen die ene realm: clients per deployment, gebruikers, rollen, identity providers, flows, en de realm verwijderen | iets buiten die realm |
| Break-glass | master | wat er misgaat handmatig rechtzetten | dagelijks gebruikt worden |

De secret van de per-realm client komt AGE-versleuteld in het projectbestand, naast de realmgegevens die daar nu al staan. Dat is dezelfde plek en dezelfde bescherming als het huidige realm-adminwachtwoord.

**Waarom een client en niet het bestaande realm-adminaccount.** Dat account is een gebruiker in master en krijgt sinds RC-16 een OTP-credential. Zou OPI daarmee inloggen, dan breekt dat zodra `KEYCLOAK_ENFORCE_ADMIN_OTP` aanstaat, want de direct-grant-flow kent dezelfde voorwaardelijke OTP-stap. Een client heeft dat probleem niet. Het OTP-werk en dit ontwerp vullen elkaar dus aan in plaats van te botsen.

**Verwijderen hoort bij de eigenaar.** OPI kan een realm nog steeds opruimen bij het verwijderen van een project, maar alleen door de sleutel van dát project te gebruiken. Een gelekte platformsleutel kan daarmee niets verwijderen, en wie één projectsleutel heeft kan precies één realm verwijderen. Dat is evenredig.

## De grens van het model: de master-admin kan altijd terug

Overdragen sluit de master-realm-admin niet buiten, en dat kan ook niet. Wie master-admin is kan een nieuwe gebruiker aanmaken, die de `{realm}-realm`-rollen geven en zo alsnog in elke realm. Dat is geen gat in dit ontwerp maar een eigenschap van Keycloak: de master-admin is superuser over alle realms, altijd.

Het onderscheid dat overblijft is wel het onderscheid dat telt. Een identiteit met alléén `create-realm` kan geen gebruikers aanmaken in master en geen rollen toekennen, want daar is `manage-users` voor nodig. Die kan dus realms maken en niet terugkomen in wat hij heeft overgedragen. Dat is precies OPI's dagelijkse identiteit, en daar werkt de compartimentering.

Wat niet in te perken valt is het menselijke `admin`-account, want dat moet break-glass blijven. En juist omdat dat de enige sleutel is die je niet kleiner kunt maken, is het de enige waar een tweede factor echt telt. OTP daarop en compartimentering doen dus verschillend werk: het een beschermt de onherleidbare superuser, het ander alles daaromheen. Geen van beide maakt de ander overbodig, en geen van beide moet verkocht worden als een totale oplossing.

## Wat dit wel en niet oplost

Wel: een gelekt projectbestand of een gelekte enkele secret raakt één realm in plaats van alle. Het dagelijkse verkeer van OPI loopt niet meer over een sleutel die alles kan. De sleutel die veel kan wordt zeldzaam gebruikt, dus die is strakker te bewaren en te roteren, en zou achter een menselijke bevestiging kunnen (zie `plans/otp-en-verhoogde-rechten.md`).

Niet: wie OPI zelf heeft, heeft de globale AGE-sleutel en daarmee elke projectsleutel en dus elke realm. Dat is de voordeur, en die blijft. Compartimenteren is desondanks de moeite waard: het helpt tegen alles wat vóór die voordeur komt, en het beperkt wat één fout in één pad kan aanrichten.

## Wat er gemeten moet worden voordat dit gebouwd wordt

1. **Welke rol staat verwijderen toe.** `realm-management` heeft losse rollen voor clients, gebruikers en identity providers, naast `manage-realm` dat over de realm zelf gaat. Welke daarvan precies het verwijderen of uitzetten van een realm toestaat, is per Keycloak-versie te controleren en hoort een meting te zijn, geen aanname. Dit raakt ook de geplande upgrade naar Keycloak 26.
2. **De rollenlijst per operatie.** OPI raakt een realm na het aanmaken nog op veertien manieren aan (deployment-clients aanmaken en verwijderen, identity providers instellen en bijwerken, browser-flows aanmaken en overriden, realm-rollen, gebruikers). Elke daarvan bepaalt een benodigde rol. Een ontbrekende rol merk je pas als een project stukloopt, dus het bewijs moet uit een echte run komen.
3. **Kan de aanmaker zijn eigen rechten echt loslaten**, en kan hij ze zichzelf daarna niet opnieuw geven. Dat laatste is de kern van de overdracht: zonder rechten om rollen toe te kennen in master kan hij niet terug.

## Volgorde

De service-account in productie aanzetten is de eerste stap en blokkeert niets; die staat nu alleen in de sandbox-overlay, dus productie draait nog volledig op het gedeelde adminwachtwoord. Daarna dit ontwerp, en pas daarna OTP of een slot op het menselijke `admin`-account, want dat is alleen veilig als OPI aantoonbaar niet meer op dat wachtwoord leunt.
