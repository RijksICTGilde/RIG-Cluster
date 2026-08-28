# Rechtenmodellen, sleutels en tokens: invalshoeken en oplossingsrichtingen

Status: **analyse, geen besluit**. De feiten waar dit op steunt staan in [rechten-inventarisatie.md](rechten-inventarisatie.md); die worden hier niet herhaald, alleen aangehaald. De aanbeveling en de fasering staan in [rechten-plan-van-aanpak.md](rechten-plan-van-aanpak.md).

Elke naam voor een rol, recht, scope, tabel of endpoint die in dit document wordt geïntroduceerd is **VOORSTEL** en als zodanig gemarkeerd. Ze staan er om het gesprek concreet te maken, niet omdat er iets besloten is.

## 1. De vraag achter de vraag

Er lopen twee dingen door elkaar.

**Wie mag wat** is een vraag over gezag. Het antwoord hangt af van een persoon, een project en een handeling, en het verandert als iemand van rol wisselt of het project verlaat.

**Waarmee bewijs je dat** is een vraag over bewijsmiddelen. Het antwoord is een cookie, een sleutel of een token, en het verandert als een geheim uitlekt of verloopt.

De projectsleutel maakt er één ding van, want wie het geheim heeft *ís* het project. Daaruit volgt vrijwel elk probleem uit de inventarisatie: er is geen rol op te hangen (want er is geen persoon), er is niets in te trekken (want intrekken zou het project zelf treffen), er is niets te loggen (want er is geen naam), en er is niets te verfijnen (want de sleutel kan niet minder zijn dan zichzelf).

Het uit elkaar trekken van die twee is dus geen opschoning maar de kern van het werk. Alles hieronder is een variant op de vraag hoe je dat doet.

## 2. Referentiemodellen

Vier modellen die dit probleem eerder hebben opgelost, met wat er hier past en wat niet. Geen encyclopedie; alleen wat een keuze beïnvloedt.

### 2.1 GitHub

**Rollen.** Een repository kent vijf vaste rollen: `read`, `triage`, `write`, `maintain`, `admin`. Ze zijn geordend, elke hogere bevat de lagere, en de gebruiker kiest er niet zelf een bundel bij elkaar. Daarnaast bestaan organisatierollen (member, owner) en teams die rollen op repositories krijgen, zodat een mens meestal via een team aan een rol komt in plaats van rechtstreeks.

**Bewijsmiddelen.** Drie soorten, met verschillende levensduur:

- *Fine-grained personal access tokens.* Verplicht een vervaldatum, met een organisatiebeleid dat de maximale levensduur begrenst (standaardbeleid: binnen 366 dagen). Ze dragen een expliciete selectie van repositories en per resourcetype een permissie (`contents: read`, `issues: write`, en zo verder). Een organisatie kan eisen dat een eigenaar elk token dat de organisatie raakt goedkeurt, en kan die tokens in één overzicht zien en intrekken.
- *GitHub Apps.* De app is een eigen principaal. Hij wordt in een organisatie of op repositories *geïnstalleerd*, en die installatie levert kortlevende installatietokens. De app heeft eigen permissies, los van welke mens hem installeerde.
- *OIDC-federatie voor Actions.* Een workflow wisselt een kortlevend, door GitHub ondertekend identiteitstoken in bij de andere partij. Er is dan helemaal geen langlevend geheim meer om te lekken.

**Wat past hier.** De drieslag *rol voor mensen, fijnmazig token voor automatisering, eigen principaal voor een applicatie* is precies de structuur die ZAD mist. Ook de verplichte vervaldatum met een centraal maximum past: die kost weinig en haalt het grootste bezwaar tegen de huidige projectsleutel weg. De gedachte "een token is nooit meer dan de mens die hem maakte" is hier direct bruikbaar.

**Wat niet past.** De teamlaag is voor een platform met een handvol projecten per persoon overbodig; YAGNI. En GitHub kan zich veroorloven dat rechten uitsluitend in een database staan, want er is geen tweede bron van waarheid. Bij ZAD staat de ledenlijst in een git-bestand, en dat verandert de rekening (zie 4).

### 2.2 Forgejo en Gitea

**Rollen.** Een team krijgt per *unit* (code, issues, pull requests, wiki, releases, packages) een toegangsmodus: geen, lezen, schrijven, beheren. De verfijning zit dus niet in meer rollen maar in het opdelen van het object in units, met een grove schaal per unit.

**Bewijsmiddelen.** Scoped tokens: een token draagt een lijst scopes van de vorm `read:repository`, `write:issue`, `read:user`.

**Wat past hier.** Het unit-idee is verrassend goed toepasbaar. ZAD heeft natuurlijke units, deployment, component, dienst, back-up, geheim, ledenlijst, en de meeste discussies over rollen gaan feitelijk over "mag deze persoon aan de geheimen komen" of "mag deze persoon een dienst aanvragen", niet over een fijnere trap. Een grove schaal per unit is een stuk goedkoper dan een volledig rechtenmodel en dekt de bekende gevallen.

**Wat niet past.** Forgejo is ook de git-server van dit platform, en de verleiding om ZAD-rechten uit Forgejo-teams af te leiden is groot. Dat zou de waarheid over een project op een derde plek zetten, naast het projectbestand en de `users`-tabel. Niet doen.

### 2.3 AWS IAM

**Model.** Een beslissing is een functie van *principal*, *action*, *resource* en *condition*. Beleid staat los van identiteiten en wordt eraan gehangen. Een expliciete `Deny` wint altijd van elke `Allow`. Rollen zijn geen bundels rechten maar identiteiten die je tijdelijk *aanneemt*, waarbij STS kortlevende credentials uitgeeft. Permission boundaries leggen een bovengrens op wat een identiteit ooit kan krijgen, ook als iemand haar later meer rechten toekent.

**Wat past hier.** Drie dingen. Ten eerste de vierslag principal/action/resource/condition: dat is exact de vorm die AuthZEN standaardiseert (zie 7), en het is de vorm waarin de rechtencatalogus uit de inventarisatie zich laat uitdrukken. Ten tweede "een rol is iets dat je aanneemt en dat kortlevende credentials oplevert": dat is precies wat een agent nodig heeft. Ten derde de permission boundary, als antwoord op de vraag "mag een agent ooit meer dan de mens die hem startte": nee, en dat leg je vast als grens, niet als afspraak.

**Wat niet past.** De volledige beleidsentaal, met wildcards, `NotAction`, resource-policies en cross-account vertrouwen, is voor een platform met vier rollen en een paar tientallen handelingen absurd zwaar. En "deny wint altijd" is alleen zinvol als er meerdere beleidsbronnen zijn die elkaar kunnen tegenspreken; die situatie moet je hier juist vermijden.

### 2.4 Kubernetes RBAC

**Model.** Werkwoorden (`get`, `list`, `watch`, `create`, `update`, `patch`, `delete`) op resourcetypes, gebonden aan een namespace of clusterbreed. Rollen zijn puur additief: er bestaat geen `deny`. Een binding koppelt een subject aan een rol.

**Wat past hier.** De namespace als scope-eenheid is al de werkelijkheid van dit platform: elk project heeft er precies één per cluster, en `_require_namespace_owned_by_project` (`opi/api/restore_router.py:46`) handhaaft dat al expliciet. Ook het puur-additieve karakter past: zonder `deny` is een beslissing altijd uit te leggen als "er was geen regel die dit toestond", en dat is een veel begrijpelijker weigering dan "er was een regel die dit verbood, ergens".

**Wat niet past.** De aanname in de opdrachtbeschrijving dat ZAD zelf al RBAC genereert voor gebruikersnamespaces is niet uitgekomen: in `operations-manager/python/manifests/` staat geen `Role`, `RoleBinding` of `ServiceAccount`. Wat er wél staat is een ArgoCD `AppProject` per project, en dát is het bestaande declaratieve autorisatieobject om op voort te bouwen. Verder is Kubernetes-RBAC een model voor toegang tot de API van het cluster, niet tot ZAD; het rechtstreeks overnemen zou betekenen dat ZAD-rechten pas gelden als iemand `kubectl` gebruikt, en dat is niet waar het probleem zit.

### 2.5 Wat de vier gemeen hebben

Alle vier scheiden ze de identiteit van het bewijsmiddel, alle vier kennen ze een kortlevend bewijsmiddel voor automatisering, en alle vier kunnen ze een bewijsmiddel intrekken zonder de identiteit te raken. Dat is de gemene deler waar ZAD op alle drie de punten van afwijkt.

## 3. Rollen versus rechten

Twee vormen, en de vraag is niet welke beter is maar wat het kost om later van gedachten te veranderen.

**Vaste rollen met een vaste bundel.** Een rol is een naam, en achter die naam zit een lijst handelingen die in de code staat. Wat je opslaat is de naam. Dit is wat er nu is, met het verschil dat de bundel nu niet bestaat maar impliciet is.

- Goedkoop: het projectbestand verandert niet van vorm, de migratie is nul.
- Begrijpelijk: een projectbeheerder ziet vier woorden en weet genoeg.
- Beperkt: elke uitzondering ("deze ene persoon mag wel back-ups terugzetten maar niet de ledenlijst wijzigen") vraagt een nieuwe rol, en rollen vermenigvuldigen zich slecht.

**Rechten als eerste klasse, rollen als voorgedefinieerde bundel.** Wat je opslaat is een verzameling rechten; een rol is een naam voor een verzameling die je vaak gebruikt.

- Flexibel: uitzonderingen kosten geen nieuwe rol.
- Duurder: het projectbestand krijgt een grotere, lastiger te reviewen vorm, en de gebruikersinterface moet iets tonen wat niet in vier woorden past.
- Riskanter: een lange lijst rechten per persoon nodigt uit tot kopieerwerk en drift, en niemand ziet meer in één oogopslag wie wat mag.

**Wat het kost om later om te draaien.** Dit is de enige reden om er nu over na te denken, dus het antwoord moet scherp:

- *Van rollen naar rechten* is goedkoop, mits de rollen vanaf dag één als bundel expliciet in de code staan. Je vervangt dan `role: admin` door de bundel die daar al bij hoorde, en elke bestaande waarde blijft geldig. Als de bundel impliciet blijft, zoals nu, is de omzetting een archeologische opgraving door 158 routes.
- *Van rechten naar rollen* is duur en gaat bijna altijd gepaard met verlies: elke persoon met een niet-standaard verzameling moet naar de dichtstbijzijnde rol worden afgerond, en dat is per definitie een verruiming of een beperking die iemand raakt.

De asymmetrie is dus duidelijk. **Begin met rollen, maar leg de bundel expliciet vast**, één plek waar staat welke handelingen `admin` omvat, en de latere keuze blijft open zonder dat er vandaag iets duurders gebouwd wordt.

## 4. Waar leggen we het vast

### 4.1 In het projectbestand

De ledenlijst staat er al (`users`), het is GitOps-zichtbaar, elke wijziging is een commit met een auteur en een tijdstip, en de review-weg is dezelfde als voor de rest van het project.

Kosten: elke wijziging is een commit plus, voor alles wat versleuteld is, een AGE-hercodering. Er is geen manier om een recht in te trekken zonder de git-geschiedenis aan te raken, en een tak die vastloopt op een merge-conflict laat een rechtenwijziging hangen. Het projectbestand is bovendien leesbaar voor iedereen met leestoegang tot `zad-projects`, en dat is een grovere kring dan "de leden van dit project".

### 4.2 In de database

Geen commit, directe intrekking, makkelijk te doorzoeken ("in welke projecten zit deze persoon"), en een natuurlijke plek voor gegevens die niet in git horen: hashes van tokens, laatst-gebruikt-tijdstippen, intrekkingen.

Kosten: de waarheid over een project staat dan op twee plekken. Vandaag is de `ProjectStore` het enige leespad naar een project (`opi/services/project_store.py`), en dat is met opzet zo gemaakt, zie `features/futures/project-file-single-path-consolidation.md` voor wat het gekost heeft om daar te komen. Een tweede bron betekent dat elke autorisatievraag twee leespaden heeft die kunnen divergeren, dat een hersteloperatie vanuit git niet meer volledig is, en dat "wat stond er vorige maand" niet meer uit de geschiedenis te beantwoorden is.

### 4.3 De vork

Dit is een echte vork met gevolgen voor de rest van de architectuur, en de eerlijke uitkomst is dat de twee dingen niet dezelfde vraag beantwoorden:

| | Projectbestand | Database |
|---|---|---|
| Wie is lid en met welke rol | past: het is een projecteigenschap, hoort bij de rest van het project, en de commit is de registratie | past niet: introduceert een tweede waarheid over hetzelfde project |
| Welke tokens bestaan en zijn ze ingetrokken | past niet: een tokenhash in git is onuitwisbaar, en intrekken moet direct werken | past: precies waar dit hoort |
| Wie is platformbeheerder | past niet: het is geen projecteigenschap | past, en beter dan de huidige env-plus-broncode |
| Welke beslissing is genomen | past niet | past, maar dat is het logboek en dat is RC-149 |

De scheidslijn loopt dus niet tussen "rechten" en "de rest", maar tussen **gezag** (projectbestand) en **bewijsmiddelen** (database). Dat is dezelfde scheiding als in 1, en het is geen toeval: het bewijsmiddel is een technisch, kortlevend, intrekbaar ding, en het gezag is een afspraak tussen mensen die in de review thuishoort.

Eén waarschuwing, want dit is een plek waar een gat een slot kan zijn: vandaag kan een projectbeheerder de ledenlijst alleen wijzigen door een commit te veroorzaken die iemand kan terugzien. Als rollen naar de database verhuizen, verdwijnt die zichtbaarheid, en dan is er een logboek nodig vóór de verhuizing, niet erna.

## 5. Tokens

### 5.1 Per persoon, per project, of allebei

**Per project** (wat er nu is): het token hoort bij een project en zegt niets over wie het gebruikt. Bruikbaar voor CI die niet aan een mens hangt, hopeloos voor attributie.

**Per persoon**: het token hoort bij een mens en erft diens rechten in alle projecten waar die mens lid van is. Attributie is perfect, maar de radius is groot: één gelekt token opent elk project van die persoon.

**Allebei, en dat is het antwoord.** Ze beantwoorden verschillende vragen en sluiten elkaar niet uit:

- Een *persoonlijk token* (VOORSTEL: `zad-pat`) is een mens die zichzelf laat vervangen door een script. Het mag nooit meer dan de mens, het versmalt tot een selectie projecten en een selectie handelingen, en het sterft zodra de mens zijn recht verliest.
- Een *projecttoken* (VOORSTEL: `zad-project-token`) is een niet-menselijke aanroeper die bij het project hoort en niet bij een persoon: de bouwstraat die een image vervangt. Het mag alleen wat het project zelf mag, versmald tot een selectie handelingen, en het heeft een uitgever die wél een mens is en die in het logboek staat.

### 5.2 De vijf eigenschappen die vandaag ontbreken

**Vervaldatum.** Verplicht, met een maximum. Zonder maximum kiest iedereen het maximum, en dat is precies het gedrag dat GitHub met een organisatiebeleid afdwingt. VOORSTEL: maximaal 90 dagen voor een persoonlijk token, maximaal 365 voor een projecttoken, beide instelbaar met een platformmaximum. Een token zonder vervaldatum moet niet aanmaakbaar zijn, niet omdat iemand het zou misbruiken, maar omdat een uitzondering die eenmaal bestaat de norm wordt.

**Scopes, en waar ze bijten.** Een scope moet bijten op het punt waar de handeling wordt uitgevoerd, niet op het punt waar de route wordt geregistreerd. Dat is het verschil tussen een lijst toegestane paden (die veroudert zodra iemand een route toevoegt) en een lijst toegestane handelingen uit de rechtencatalogus (die niet veroudert, want een nieuwe route moet een handeling kiezen). Concreet: de scope noemt handelingen, het handhavingspunt vertaalt een binnenkomend verzoek naar een handeling, en een route zonder handeling is een fout bij het opstarten, geen stille doorlaat.

**Rotatie zonder onderbreking.** Twee tokens tegelijk geldig, een nieuw token aanmaken vóór het oude vervalt, en de aanroeper wisselt om wanneer het uitkomt. Dit vraagt dat tokens meervoudig zijn, precies wat de huidige `config.api-key`, één veld met één waarde, onmogelijk maakt.

**Intrekking.** Direct, per token, zonder het project te raken en zonder commit. Dit is het argument dat de tokentabel in de database beslecht (zie 4.3).

**Zichtbaarheid.** Vandaag kan elke `admin` of `owner` de projectsleutel teruglezen op de detailpagina, omdat hij AGE-versleuteld in het bestand staat en dus per definitie terug te halen is. Dat is de omgekeerde wereld: een uitgegeven bewijsmiddel hoort na uitgifte door niemand meer leesbaar te zijn, ook niet door degene die het uitgaf. Gehashte opslag maakt dat afdwingbaar in plaats van afgesproken. Het kost iets: wie zijn token kwijt is, moet een nieuw token maken in plaats van het oude opzoeken. Dat is de bedoeling.

### 5.3 Twee paden voor de uitgifte

**Keycloak laat uitgeven.** Per project een client met client credentials; delegatie via token exchange. Voordeel: er staat al een Keycloak, tokens zijn geverifieerde JWT's met een handtekening, verval zit in het protocol, en intrekking is een bestaand begrip. Nadeel: elk project een Keycloak-client erbij is een aanzienlijke uitbreiding van wat OPI in Keycloak beheert, terwijl `features/keycloak-additional-clients.md` laat zien hoeveel dat al is. Bovendien woont de beleidsdata, wie is lid met welke rol, in het projectbestand, en die zou dan als claim in het token moeten worden gemapt, wat betekent dat een rolwijziging pas werkt als het token vervalt.

**Eigen tokentabel met gehashte opslag.** Een tabel met tokenhash, eigenaar, scopes, project, vervaldatum, intrekkingsmoment en laatst-gebruikt. Voordeel: intrekking werkt onmiddellijk, scopes komen uit de eigen rechtencatalogus in plaats van uit een claimmapping, en er is geen afhankelijkheid van Keycloak op het hete pad van elke API-aanroep. Nadeel: zelf bouwen betekent zelf goed doen, constante-tijdvergelijking, een hashkeuze die geen wachtwoordhash hoeft te zijn maar wel bestand tegen een gelekte database, en een opruimtaak voor verlopen rijen.

**Afweging.** Voor *machine-naar-machine binnen ZAD* wint de eigen tabel, omdat intrekking en scopes daar de doorslaggevende eigenschappen zijn en beide in het Keycloak-pad juist het zwakst zijn. Voor *een mens die zich aanmeldt* is Keycloak al de weg en moet dat zo blijven. De grens loopt dus bij "wordt er een mens geauthenticeerd of niet", en dat is dezelfde grens als in 1.

### 5.4 Wat er met de bestaande projectsleutels gebeurt

Er zijn projecten die vandaag draaien en die de sleutel in een pijplijn hebben staan. Die mogen niet omvallen. De volgorde:

1. De bestaande `config.api-key` blijft geldig en blijft werken. Er verandert niets aan het bestand.
2. Naast de sleutel komt de nieuwe tokenweg. Vanaf dat moment kan een project een projecttoken aanmaken met scopes en een vervaldatum.
3. De handhaving vertaalt beide naar hetzelfde begrip: de oude sleutel is intern een token met *alle* scopes en *geen* vervaldatum. Zo hoeft het handhavingspunt maar één ding te kennen, en is het verschil tussen oud en nieuw meteen zichtbaar in het logboek.
4. Wanneer een project geen aanroepen meer met de oude sleutel doet, meetbaar, want de vertaling uit stap 3 legt dat vast, kan de sleutel worden ingetrokken.
5. Pas als geen enkel project hem nog gebruikt, verdwijnt het veld.

Stap 3 is de kern en tegelijk het punt waar een verruiming op de loer ligt: de oude sleutel *krijgt* in die vertaling expliciet alle scopes, en dat is niet meer dan hij nu al heeft, maar het staat er dan zwart op wit, en dat maakt het aantrekkelijk om er "voor de zekerheid" iets bij te doen. Dat mag niet: de vertaling moet exact de huidige 46 handelingen dekken en geen enkele meer.

## 6. Agents

Een agent is geen nieuw soort bewijsmiddel maar een nieuw soort aanroeper, en de vraag die hij stelt is: namens wie handel je. Er zijn twee legitieme antwoorden en ze beantwoorden verschillende vragen. Ze door elkaar halen is de manier waarop dit misgaat.

### 6.1 De agent handelt namens een persoon

De agent is een verlengstuk. Iemand start hem, hij doet iets, en de verantwoordelijkheid ligt bij die persoon.

- **Rechten:** gedelegeerd, en versmald tot minder dan de persoon zelf mag. Nooit gelijk, want dan is delegatie zinloos, en nooit meer, want dan is de persoon niet meer de bovengrens. Dit is de permission boundary uit 2.3.
- **Levensduur:** kort. Een agent-sessie is minuten tot uren, geen maanden. Een langlevend gedelegeerd token is een persoonlijk token met een dun laagje eromheen.
- **Sterfte:** zodra de persoon zijn recht verliest, sterft het token. Dat is een harde eis en hij is niet gratis: hij betekent dat het handhavingspunt bij elke beslissing de *huidige* rechten van de persoon raadpleegt en niet die uit het token. Een gedelegeerd token draagt dus een verwijzing naar de persoon, niet een kopie van diens rechten.
- **Attributie:** het logboek noemt beide. VOORSTEL: `subject` is de agent, en de persoon staat als `on_behalf_of` in het verzoek. Alleen de persoon noemen verbergt dat er een machine handelde; alleen de agent noemen verbergt wie verantwoordelijk is.
- **Intrekking:** twee knoppen. De agent-sessie intrekken raakt één agent. De rechten van de persoon intrekken raakt al zijn agents tegelijk, en dat is de knop die je bij een incident nodig hebt.

### 6.2 De agent is een eigen principaal

De agent hangt aan een project, zoals een GitHub App-installatie of een AWS-rol. Er is geen mens achter, alleen een mens die hem heeft aangezet.

- **Rechten:** eigen rechten, toegekend aan het project, niet afgeleid van een persoon. Ze kunnen dingen bevatten die geen enkele mens heeft, een agent die alleen images mag vervangen en verder niets is strikter dan elke rol.
- **Levensduur:** de installatie is langlevend, de tokens zijn kort. Dat is de scheiding die GitHub Apps maken en het is de reden dat een gelekt installatietoken beperkte schade doet.
- **Sterfte:** hij sterft als de installatie wordt ingetrokken, niet als een mens vertrekt. Dat is een eigenschap, geen gebrek: een bouwstraat hoort niet stil te vallen omdat een collega van baan wisselt. Maar het betekent wel dat er een eigenaarschapsvraag ligt, wie beheert deze agent als degene die hem aanzette weg is, en die moet beantwoord zijn vóórdat de eerste agent bestaat.
- **Attributie:** het logboek noemt de agent en de installatie, plus wie hem heeft aangezet en wanneer.
- **Mag hij meer dan de mens die hem startte?** Voor deze vorm: ja, en dat is legitiem, want hij is niet gestart namens die mens. Maar dan moet de handeling van het *aanzetten* zwaarder bewaakt zijn dan de handelingen die hij vervolgens doet. Anders is "een agent installeren" een rechtenverhoging in vermomming.

### 6.3 Wat vandaag de agent-ingang is

De inventarisatie is hier onverbiddelijk: er is geen agent-vriendelijke ingang met een eigen identiteit. Wat er is, is de projectsleutel, en die opent alle 46 mutatieroutes van een project zonder rol, verval, intrekking of naam (zie [rechten-inventarisatie.md](rechten-inventarisatie.md), 6.11 en 8.7). Een agent die vandaag iets moet doen, krijgt dus de sleutel, en daarmee is de agent het project.

De opdrachtbeschrijving noemde hier een bearer-token-weg voor het opvragen en aanmaken van projecten. **Die bestaat in deze tak niet** (zie de inventarisatie, 1). Er is dus geen bestaande weg om op voort te bouwen en ook geen bestaande weg om te repareren; er is alleen de sleutel. Dat maakt de agentvraag urgenter, niet minder urgent: de eerste agent die op dit platform iets doet, doet het met het volledige gezag van het project, en niets legt vast dat hij het was.

### 6.4 De regel die beide vormen deelt

Een agent moet in het verzoek zichtbaar maken *dat hij een agent is*. Niet omdat een agent minder te vertrouwen is, maar omdat een beslissing anders niet uit te leggen valt: "deze deployment is verwijderd door jan@rijksoverheid.nl" is een ander verhaal dan "deze deployment is verwijderd door een agent die jan had gestart". Dat hoort in de context van het verzoek (zie 7.2), en het is een van de weinige dingen die je niet met terugwerkende kracht kunt toevoegen.

## 7. AuthZEN, uitgewerkt

Dit is de richting waar expliciet naar gevraagd is, dus hij wordt hier niet gewogen maar uitgewerkt tot een inrichtingsvoorstel, met daarnaast een eerlijk oordeel over wat hij niet oplost.

### 7.1 Wat AuthZEN is, geverifieerd

De **Authorization API 1.0** van de OpenID Foundation is in januari 2026 als Final Specification aangenomen, met 81 stemmen voor, 1 tegen en 25 onthoudingen (107 stemmen, 28,3% van 378 leden, ruim boven het quorum van 20%). Bron: <https://openid.net/authorization-api-1-0-final-specification-approved/>.

Hij standaardiseert het gesprek tussen het handhavingspunt (PEP) en het beslispunt (PDP), en **nadrukkelijk niet het beleidsmodel**. De vorm (<https://openid.github.io/authzen/>):

```
POST /access/v1/evaluation
{
  "subject":  { "type": "...", "id": "...", "properties": { ... } },
  "action":   { "name": "...",             "properties": { ... } },
  "resource": { "type": "...", "id": "...", "properties": { ... } },
  "context":  { ... }
}
->
{ "decision": true|false, "context": { ... } }
```

`subject.type`, `subject.id`, `action.name`, `resource.type` en `resource.id` zijn verplicht; `properties` en `context` zijn optioneel. De reden bij een weigering gaat in het optionele `context` van het antwoord; de specificatie noemt daarvoor onder meer `reason_admin` en `reason_user` als gebruikelijke sleutels.

Naast `/access/v1/evaluation` kent 1.0 nog vier endpoints: `/access/v1/evaluations` (meerdere beslissingen in één aanroep), en drie zoek-endpoints, `/access/v1/search/subject`, `/search/resource` en `/search/action`. Die laatste drie zijn hier onverwacht relevant en komen terug in 7.5.

**Keycloak** ondersteunt AuthZEN sinds versie 26.7.0, als **experimentele** functie: de Evaluation API, de batch-variant Evaluations API, en ontdekking via `.well-known/authzen-configuration` per realm. Bron: <https://www.keycloak.org/2026/05/authzen-as-experimental-feature>.

**Logius** heeft een **AuthZEN NL GOV**-profiel in werkversie, gedateerd 13 augustus 2026, versie 1.0 van de API-beschrijving, met de expliciete status "draft that could be altered, removed or replaced". Belangrijk detail: het profiel beschrijft zichzelf als een *fork* van AuthZEN **draft 04**, niet van de aangenomen 1.0. Het voegt Nederlandse elementen toe: een optionele `processing_activity_id` die naar het verwerkingsregister verwijst, een optionele `algorithm_id` die naar het Algoritmeregister verwijst, aandacht voor MIM-modellering en JSON-LD, en W3C Trace Context via `traceparent`/`tracestate`. Bron: <https://logius-standaarden.github.io/authzen-nlgov/>.

Dat het NL GOV-profiel op een oudere draft staat is geen detail: wie zich vandaag op het profiel vastlegt, legt zich vast op iets dat nog moet meebewegen met de aangenomen versie.

### 7.2 De afbeelding van ZAD op het model

Dit is het scharnierpunt: zonder deze tabel is AuthZEN een lege huls. Alle namen hieronder zijn **VOORSTEL**.

**Subjecten**: wie vraagt iets.

| `subject.type` | `subject.id` | Waar hij vandaan komt vandaag | Wat we van hem weten |
|---|---|---|---|
| `user` | e-mailadres | sessiecookie, `opi/middleware/authorization.py:40` | naam, allowlist-status, platformbeheerderstatus, rol per project |
| `project-key` | projectnaam | `X-API-Key`, `opi/api/endpoint_util.py:45-49` | uitsluitend het project; geen persoon, geen rol |
| `platform-key` | `admin` of `master` | `X-API-Key` tegen een env-waarde | niets |
| `agent` | agent-identifier | bestaat niet | zie 6 |

De rij `agent` staat er met opzet in terwijl hij niet bestaat: het is de enige plek in dit document waar zichtbaar wordt dat het model ruimte heeft voor iets dat het platform nog niet kent.

**Handelingen**: `action.name` komt rechtstreeks uit de rechtencatalogus (inventarisatie, 6). VOORSTEL voor de vorm: `<object>.<werkwoord>`, kleine letters, streepjes binnen een woord.

| Voorbeeld | Wat het is | Vandaag bereikbaar via |
|---|---|---|
| `project.read` | projectdetails lezen | `GET /projects/details/{p}` |
| `project.delete` | project verwijderen | `POST /projects/delete/{p}`, `DELETE /api/projects/{p}` |
| `deployment.update-image` | image van een deployment vervangen | `PUT /api/v2/projects/{p}/deployments/{d}/image` |
| `deployment.delete` | deployment verwijderen | `DELETE /api/v2/projects/{p}/{d}` |
| `component.create` | component toevoegen | `POST /api/v2/projects/{p}/components` |
| `service.add` | dienst aan een project toevoegen | `POST /api/v2/projects/{p}/services` |
| `backup.restore` | back-up terugzetten | `POST /api/v1/restore/project/{p}` |
| `secret.read` | projectsleutel of omgevingsvariabelen lezen | detailpagina, `opi/web/router.py:1010` |
| `member.update` | ledenlijst wijzigen | bewerkvenster, `opi/web/router_detail_edit.py:743` |
| `platform.reconcile` | reconciliatie starten | `POST /api/v2/admin/reconciliation/trigger` |

De eis die hierbij hoort: **elke route wijst precies één handeling aan, en een route zonder handeling start niet op**. Dat is wat een catalogus tot mechanisme maakt in plaats van tot momentopname.

**Resources**: waar de handeling op slaat.

| `resource.type` | `resource.id` | Eigenschappen die het beslispunt nodig heeft |
|---|---|---|
| `platform` | clusternaam | - |
| `project` | projectnaam | `members` (e-mail plus rol) |
| `deployment` | `<project>/<deployment>` | `project`, `cluster`, `namespace` |
| `component` | `<project>/<component>` | `project` |
| `service` | `<project>/<dienst>` | `project` |
| `backup` | `<cluster>/<namespace>/<snapshot>` | `project`, `namespace` |
| `secret` | `<project>/<soort>` | `project` |
| `task` | taak-UUID | `project` |
| `user` | e-mailadres | - |
| `subdomain` | subdomeinnaam | `project` |

De eigenschap `project` op bijna elk type is geen opvulling: hij is precies de reden dat de vijf voortgangsroutes uit gat 8.2 misgaan. Een `task` zonder `project` is niet te autoriseren, en in dit model is dat zichtbaar in plaats van vergeten.

**Context**: de omstandigheden.

| Sleutel | Waarde | Waarom |
|---|---|---|
| `cluster` | `settings.CLUSTER_MANAGER` | elke OPI beheert alleen zijn eigen cluster; een verzoek voor een ander cluster hoort te weigeren |
| `channel` | `ui` of `api` | een handeling die via de interface een bevestigingsvenster kent, mag via de API niet stilzwijgend gebeuren |
| `via_agent` | boolean | zie 6.4 |
| `on_behalf_of` | e-mailadres | alleen bij een gedelegeerde agent, zie 6.1 |
| `traceparent` | W3C Trace Context | vereist door het NL GOV-profiel, en de koppeling met het logboek |

### 7.3 Drie uitgewerkte voorbeelden

Alle drie gebaseerd op bestaande routes. Bij elk staat wat er vandaag gebeurt, zodat het verschil zichtbaar is.

**Voorbeeld 1, een developer werkt een image bij.**

Vandaag: via de interface loopt dit via het veld "Container image" in het deployment-bewerkvenster (`opi/forms/visualizers/fields/deployments.py:160`), dat `admin`/`owner` eist, dus de developer krijgt daar 403. Via de API is de projectsleutel de enige weg, en die kent geen rol; de developer kan de sleutel niet lezen, maar wie hem eenmaal heeft kan alles. Er is dus geen stand waarin deze persoon precies deze ene handeling mag.

```json
POST /access/v1/evaluation
{
  "subject":  { "type": "user", "id": "jan@rijksoverheid.nl" },
  "action":   { "name": "deployment.update-image" },
  "resource": { "type": "deployment", "id": "algor-odc/deployment-1",
                "properties": { "project": "algor-odc", "cluster": "odcn-production" } },
  "context":  { "cluster": "odcn-production", "channel": "api" }
}
```

```json
{
  "decision": false,
  "context": {
    "reason_user":  "Uw rol in dit project (developer) staat het bijwerken van images niet toe.",
    "reason_admin": "role=developer; deployment.update-image vereist admin of owner"
  }
}
```

**Voorbeeld 2, een projectsleutel verwijdert een deployment.**

Vandaag: `DELETE /api/v2/projects/algor-odc/deployment-1` met de juiste sleutel slaagt, zonder rol, zonder naam in het logboek.

```json
POST /access/v1/evaluation
{
  "subject":  { "type": "project-key", "id": "algor-odc",
                "properties": { "token_id": "tok_7f3a", "scopes": ["deployment.*"] } },
  "action":   { "name": "deployment.delete" },
  "resource": { "type": "deployment", "id": "algor-odc/deployment-1",
                "properties": { "project": "algor-odc", "cluster": "odcn-production" } },
  "context":  { "cluster": "odcn-production", "channel": "api" }
}
```

```json
{ "decision": true, "context": { "reason_admin": "token tok_7f3a heeft scope deployment.*; resource.project == subject.id" } }
```

De beslissing is `true`, precies zoals vandaag. Wat verandert is dat er nu drie dingen vastliggen die er nu niet zijn: *welk* token het was, dat de scope het toestond, en dat het project van de resource overeenkwam met het project van het token. Zou dezelfde sleutel `deployment.delete` op een ánder project proberen, dan is het `false` op de laatste voorwaarde, en dat is dezelfde controle die `_require_namespace_owned_by_project` (`opi/api/restore_router.py:46`) nu op vijf plekken met de hand doet en op de overige 41 routes niet nodig heeft omdat het projectpad al in de URL zit.

**Voorbeeld 3, een platformbeheerder start een reconciliatie.**

Vandaag: `POST /api/v2/admin/reconciliation/trigger` met de `ADMIN_API_KEY`. De aanroeper is anoniem, en zonder ingestelde sleutel antwoordt de route 501.

```json
POST /access/v1/evaluation
{
  "subject":  { "type": "user", "id": "beheerder@rijksoverheid.nl",
                "properties": { "platform_admin": true } },
  "action":   { "name": "platform.reconcile" },
  "resource": { "type": "platform", "id": "odcn-production" },
  "context":  { "cluster": "odcn-production", "channel": "api", "via_agent": false }
}
```

```json
{ "decision": true, "context": { "reason_admin": "platform_admin=true; platform.reconcile vereist platform-admin" } }
```

En dezelfde aanroep door een agent die deze beheerder heeft gestart:

```json
"subject": { "type": "agent", "id": "agent:ci-runner-4" },
"context": { "cluster": "odcn-production", "channel": "api",
             "via_agent": true, "on_behalf_of": "beheerder@rijksoverheid.nl" }
```

```json
{
  "decision": false,
  "context": {
    "reason_user":  "Deze handeling kan niet door een agent worden uitgevoerd.",
    "reason_admin": "platform.reconcile is uitgesloten van delegatie"
  }
}
```

Dat laatste is het punt van 6.4: zonder `via_agent` in de context is dit onderscheid niet te maken, en dan is er geen manier om te zeggen dat sommige handelingen een mens vereisen.

### 7.4 Waar het beslispunt draait

Drie vormen, en de vraag "wat breekt er als het beslispunt onbereikbaar is" is hier zwaarder dan elders, want elk cluster draait zijn eigen OPI zonder gedeelde toestand.

| | In het OPI-proces zelf | Apart component (OPA, Cedar, Topaz) | Keycloak |
|---|---|---|---|
| Latentie per beslissing | geen netwerkhop | een netwerkhop, elke beslissing | een netwerkhop, elke beslissing |
| Bij onbereikbaarheid | onmogelijk: het is hetzelfde proces | het hele platform staat stil, want een dichte deur is het enige veilige antwoord | idem, en Keycloak is dan een tweede kritiek pad naast de bestaande SSO-afhankelijkheid |
| Beleidsdata | direct beschikbaar: `ProjectStore` staat ernaast | moet erheen, per verzoek of als geladen beleid (zie 7.5) | moet erheen als claims of via een PIP-koppeling |
| Per cluster | vanzelf, want elke OPI is er al één | een extra component per cluster, of één centraal met een cross-cluster afhankelijkheid | Keycloak staat er al per omgeving |
| Rijpheid | wat je zelf schrijft | volwassen, elk met een eigen beleidsentaal | experimenteel sinds 26.7.0 |
| Kosten om te bouwen | laag: een functie met een vaste verzoekvorm | een component, beleid in een nieuwe taal, uitrol per cluster, versiebeheer van beleid | een client-configuratie per project plus claimmapping |
| Kosten om later te wisselen | laag, mits de verzoekvorm van dag één AuthZEN is | - | - |

**De sleutelzin staat in de laatste rij.** Een beslispunt *in* het OPI-proces dat alleen de AuthZEN-verzoekvorm aanneemt en een AuthZEN-antwoord teruggeeft, maakt van "externaliseren" later een transportwissel, een HTTP-aanroep in plaats van een functieaanroep, in plaats van een herontwerp. Dat is de goedkoopste manier om de deur open te houden zonder vandaag een netwerkhop, een tweede beleidsentaal en een uitrol per cluster te kopen.

Voor een platform waarvan elke cluster zijn eigen OPI draait en waar de beleidsdata in het projectbestand van dat cluster staat, is een externe PDP bovendien inhoudelijk lastig te verdedigen: hij zou per beslissing gegevens moeten ophalen die de aanroeper al in het geheugen heeft.

### 7.5 Waar het beleid vandaan komt

Het antwoord hoort uit het projectbestand te komen, want daar staan de leden. Twee vormen:

**Per verzoek ophalen (PIP-model).** Het beslispunt vraagt bij elke beslissing de ledenlijst op. Binnen het proces is dat gratis: `get_project_store().get(name)` leest uit de cache. Bij een extern beslispunt is het een tweede netwerkhop bovenop de eerste, of een terugroep van het beslispunt naar OPI, en dán wordt een externe PDP écht duur, want elke beslissing kost twee hops en een cache-invalidatievraag erbij.

**Bij elke projectwijziging laden (PAP-model).** Elke commit in `zad-projects` duwt het beleid naar het beslispunt. Sneller bij het beslissen, maar het introduceert een venster waarin het beslispunt een oudere ledenlijst heeft dan het projectbestand. Voor het toevoegen van een lid is dat onschuldig; voor het *verwijderen* van een lid is het precies verkeerd: iemand die uit een project is gezet, houdt zijn rechten tot de synchronisatie klaar is. Wie dit kiest, moet die vertraging benoemen en begrenzen, niet negeren.

**Voor een beslispunt binnen het proces valt de keuze weg**, want de bron staat ernaast en is al de enige leesweg. Dat is een tweede, onafhankelijk argument voor de eerste vorm uit 7.4.

De zoek-endpoints van AuthZEN 1.0 verdienen hier een aparte vermelding, want ze passen op twee bestaande schermen. `/access/v1/search/resource` beantwoordt "welke projecten mag deze persoon zien", vandaag opgelost door in `opi/web/router.py:2540` over alle projecten te lopen en per stuk `is_user_authorized_for_project` aan te roepen. En `/access/v1/search/action` beantwoordt "wat mag deze persoon hier", vandaag opgelost door dertien Jinja2-blokken die zelf `user_role in ["admin", "owner"]` uitrekenen. Beide zijn dus geen nieuwe functionaliteit maar een bestaande, met de hand geschreven berekening die een naam krijgt. Dat maakt ze een goedkope tweede stap en tegelijk het bewijs dat de modellering klopt: als `search/action` niet exact de dertien templateblokken kan reproduceren, klopt de catalogus niet.

### 7.6 Wat AuthZEN niet oplost

Dit hoort er expliciet bij te staan, want de standaard wordt makkelijk aangezien voor het rechtensysteem.

- **Er is niets te evalueren zonder rechtencatalogus.** AuthZEN standaardiseert de vorm van de vraag, niet de inhoud. Zonder de handelingen uit [rechten-inventarisatie.md](rechten-inventarisatie.md) is er geen `action.name` om in te vullen. Dit is dus onder alle omstandigheden een *latere* stap dan document 1, en wie hem eerder zet, bouwt een envelop zonder brief.
- **Geen rollenmodel.** AuthZEN zegt niets over of `member` minder is dan `developer`, of wat `admin` omvat. Dat blijft een eigen keuze (zie 3).
- **Geen tokenbeheer.** Vervaldata, scopes, rotatie, intrekking en gehashte opslag komen er niet uit. Het beslispunt krijgt een subject aangereikt; wie dat subject vaststelde en waarmee, is de vraag uit 5 en blijft dat.
- **Geen antwoord op wie een sleutel mag zien.** Dat is een gewone handeling (`secret.read`) in de catalogus, en AuthZEN kan er een beslissing over nemen, maar alleen als iemand eerst opschrijft wat het antwoord hoort te zijn.
- **Geen logboek.** De standaard maakt een beslissing wél vastlegbaar, maar legt niets vast. Zie 8.
- **Geen bescherming tegen een ontbrekend handhavingspunt.** Een route die het beslispunt niet aanroept, is nergens zichtbaar. Dat blijft precies het probleem uit gat 8.5 en 8.6 van de inventarisatie, tenzij het aanroepen ervan afdwingbaar wordt gemaakt, een route zonder handeling die niet opstart. Dat is een eigen maatregel, geen eigenschap van de standaard.

### 7.7 Het migratiepad naar één handhavingspunt

Elf aanroepen van `require_project_edit_access`, vijftien van `_require_project_member_access`, tien van drie identieke `_require_admin`-kopieën, zes handgeschreven rolcontroles, vijftien handgeschreven lidmaatschapscontroles en dertien templateblokken worden niet in één keer een beslispunt. De weg zonder breuk:

1. **Voeg het beslispunt toe zonder er iets van af te laten hangen.** Eén functie die de AuthZEN-verzoekvorm aanneemt en een antwoord teruggeeft. Nog geen enkele route gebruikt hem.
2. **Laat de bestaande helpers hem aanroepen en het antwoord alleen vergelijken.** `require_project_edit_access` blijft zelf beslissen, roept daarnaast het beslispunt aan, en logt elk verschil. Zolang er verschillen zijn, klopt de modellering niet.
3. **Zwijgen is het bewijs.** Als er over een representatieve periode geen verschil meer wordt gelogd, kan de helper zijn eigen oordeel laten vallen en het antwoord van het beslispunt volgen. Vanaf dat moment is de gate op elf plekken tegelijk verplaatst zonder dat er iets van gedrag veranderde.
4. **Herhaal per helper**, in volgorde van aantal aanroepen: eerst `_require_project_member_access`, dan de drie `_require_admin`-kopieën (die daarmee vanzelf één worden), dan de handgeschreven controles.
5. **Sluit af met de templates.** Die vragen `search/action` in plaats van zelf te rekenen. Dit is bewust de laatste stap: het is de enige waar een fout zichtbaar wordt als een verdwenen knop in plaats van als een 403, en dat wil je pas als je het beslispunt vertrouwt.

**Waaraan je merkt dat het klopt:** in stap 2 daalt het aantal gelogde verschillen naar nul en blijft het daar; in stap 5 reproduceert `search/action` exact de dertien bestaande templateblokken. Beide zijn meetbaar en geen van beide vraagt om een gedragsverandering die je moet terugdraaien als het misgaat.

## 8. Het logboek

Er loopt een parallelle taak (**RC-149**) over gebeurtenissen vastleggen en melden. Het logboekdeel hoort daar en wordt hier niet dubbel gebouwd. Wat een *autorisatiebeslissing* extra vraagt bovenop een gewone gebeurtenis:

- **De vraag én het antwoord**, niet alleen de uitkomst. Een gebeurtenis "deployment verwijderd" is genoeg om te weten wat er gebeurde; om te weten *waarom het mocht* moet het verzoek erbij: subject, action, resource, context.
- **Ook de weigeringen.** Een gewone gebeurtenis ontstaat als er iets gebeurt. Een autorisatiebeslissing die `false` teruggeeft is juist dán interessant, en dat is precies het gebeurtenistype dat een gebeurtenissysteem zonder aanvullende afspraak nooit ziet.
- **De reden.** `reason_admin` uit het antwoord, zodat een weigering achteraf uit te leggen is zonder de code te lezen.
- **Correlatie.** Eén handeling kan meerdere beslissingen kosten; ze horen aan één trace te hangen.

De **Authorization Decision Log**-werkversie van Logius is hier het aanknopingspunt. Geverifieerd op <https://logius-standaarden.github.io/authorization-decision-log/>: status draft, versie 0.0.1, gedateerd 16 juli 2026, met als verplichte velden `trace_id` (16 bytes), `span_id` (8 bytes), `event_name`, `timestamp` en `status`, `parent_span_id` verplicht behalve voor de wortel van een trace, en `attributes`, `resource` en `body` optioneel. Het model is bewust vormcompatibel met OpenTelemetry en OTLP wordt aanbevolen als transport, maar het schrijft geen telemetrieraamwerk voor.

Let op: dit wijkt af van oudere samenvattingen van deze standaard, die `type`, `request` en `response` als verplichte velden noemen. Wie zich hierop baseert, moet de live werkversie lezen en niet een afgeleide beschrijving.

De praktische consequentie voor RC-149: als dat werk OpenTelemetry-vormige gebeurtenissen oplevert met `trace_id` en `span_id`, dan is het autorisatiedeel een gebeurtenistype erbij en geen tweede systeem. Als het een eigen vorm kiest, wordt dit later een conversie. Dat is het enige punt waar de twee taken elkaar echt raken, en het is de moeite waard om het nu af te stemmen in plaats van straks.

## 9. Wat hier niet bij hoort

- **De rechten van OPI zelf tegenover Keycloak.** De opdrachtbeschrijving verwijst hiervoor naar `features/futures/keycloak-rechten-overdragen.md`. **Dat document bestaat niet in deze tak.** Wat er wel is over OPI's rol tegenover Keycloak staat verspreid in `features/keycloak-additional-clients.md`, `features/keycloak-realm-roles.md`, `features/keycloak-auto-link.md` en `operations-manager/python/docs/KEYCLOAK_SETUP.md`. Het onderwerp valt buiten dit document; als er behoefte is aan een samenhangend beeld, is dat een eigen taak.
- **De toegangsmuur vóór gebruikersapplicaties** (`features/authorization-wall.md`). Dat gaat over wie een gedeployde applicatie mag bereiken, niet over wie iets in ZAD mag. Zie ook 3.5 van de inventarisatie: die as heeft eigen rollen met dezelfde namen, en verwarring is daar de valkuil.
- **Rolcontrole op veldniveau in het formulierensysteem** (`features/futures/form-field-rbac.md`). Dat is de uitvoering van een rechtenmodel binnen één formulier en veronderstelt het model dat hier ontworpen wordt. Het hoort na dit werk, niet ervoor.
- **Scheiding tussen projecten op infrastructuurniveau** (`features/futures/tenant-isolation-followups.md`). Aanpalend, maar het gaat over namespaces, netwerkbeleid en gelijktijdigheid, niet over gezag.

## Verwante documenten

- [rechten-inventarisatie.md](rechten-inventarisatie.md), de gemeten uitgangssituatie
- [rechten-plan-van-aanpak.md](rechten-plan-van-aanpak.md), aanbeveling, fasering, migratie, te nemen besluiten
- [form-field-rbac.md](form-field-rbac.md), [tenant-isolation-followups.md](tenant-isolation-followups.md), [project-file-single-path-consolidation.md](project-file-single-path-consolidation.md)
- `features/authorization-wall.md`, `features/invite-system.md`, `features/keycloak-realm-roles.md`, `features/zad-external-user-support.md`, `features/user-admin-crud.md`, `features/metrics-endpoint-security.md`
