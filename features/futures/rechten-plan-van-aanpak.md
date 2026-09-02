# Rechten in ZAD: plan van aanpak

Status: **voorstel, nog geen besluit**. De feiten staan in [rechten-inventarisatie.md](rechten-inventarisatie.md), de afwegingen in [rechten-modellen-en-tokens.md](rechten-modellen-en-tokens.md). Dit document doet de aanbeveling, de fasering, de migratie en de vraag welke besluiten een mens moet nemen voordat er iets gebouwd wordt.

Elke zelfbedachte naam is **VOORSTEL**, net als in document 2.

## 1. De aanbeveling in het kort

**Scheid gezag van bewijsmiddel, maak de rechten expliciet als handelingen, en zet er één beslispunt achter dat de AuthZEN-verzoekvorm spreekt maar binnen het OPI-proces draait.**

Uitgeschreven:

1. Elke route wijst precies één handeling aan uit een vaste lijst. Een route zonder handeling start niet op.
2. Er is één functie die beslist, en die neemt een AuthZEN-verzoek aan en geeft een AuthZEN-antwoord terug. Hij draait in het proces, niet over het netwerk.
3. Rollen blijven rollen, maar hun bundel handelingen staat expliciet op één plek in plaats van impliciet verspreid over 158 routes.
4. Lidmaatschap en rol blijven in het projectbestand. Tokens, hun scopes en hun intrekking komen in de database.
5. Elk token heeft een verplichte vervaldatum, scopes uit dezelfde handelingenlijst, en is na uitgifte door niemand meer leesbaar.
6. Een agent maakt in het verzoek zichtbaar dát hij een agent is, en of hij namens iemand handelt.

**Waarom AuthZEN als vorm en niet als infrastructuur.** De standaard is aangenomen, hij past exact op wat hier nodig is, en de verzoekvorm kost niets extra's ten opzichte van een zelfbedachte functiehandtekening. Externaliseren naar een apart beslispunt kost wél iets, een netwerkhop per beslissing, een tweede beleidsentaal, een uitrol per cluster, en een component waarvan de onbereikbaarheid het hele platform stillegt, en levert hier niets op, omdat de beleidsdata (de ledenlijst uit het projectbestand) al naast de beslisser staat. Door de vorm nu vast te leggen wordt externaliseren later een transportwissel in plaats van een herontwerp. Dat is de hele winst, en hij is gratis.

## 2. Wat afvalt, en waarom, in één zin per richting

| Afgevallen richting | Waarom |
|---|---|
| Een extern beslispunt (OPA, Cedar, Topaz) nu bouwen | een netwerkhop en een tweede beleidsentaal per beslissing, terwijl de beleidsdata al in het proces zit en elke cluster zijn eigen OPI draait. |
| Keycloak als beslispunt | de ondersteuning is experimenteel sinds 26.7.0, en de beleidsdata woont hier in het projectbestand en niet in Keycloak. |
| Nu al het NL GOV AuthZEN-profiel volgen | het is een werkversie van 13 augustus 2026 die zichzelf beschrijft als fork van AuthZEN draft 04, dus vastleggen betekent vastleggen op iets dat nog moet meebewegen met de aangenomen 1.0. |
| Rechten als eerste klasse, met rollen als optionele bundel erbovenop | duurder in opslag, review en interface, terwijl de omgekeerde stap (van expliciete rollen naar rechten) later goedkoop blijft. |
| Rollen naar de database verplaatsen | zet een tweede waarheid over een project naast het projectbestand, terwijl er juist met moeite één leespad is gemaakt. |
| ZAD-rechten afleiden uit Forgejo-teams | zou de waarheid over een project op een derde plek zetten. |
| Kubernetes-RBAC als ZAD-rechtenmodel overnemen | dat model regelt toegang tot de cluster-API, niet tot ZAD, en ZAD genereert vandaag geen RBAC. |
| Tokens uitsluitend door Keycloak laten uitgeven | intrekking en scopes zijn hier de doorslaggevende eigenschappen, en dat zijn juist de zwakste kanten van die weg. |
| Tokens uitsluitend per persoon, of uitsluitend per project | ze beantwoorden verschillende vragen; kiezen betekent één van beide onbeantwoord laten. |
| De projectsleutel in één keer vervangen | er draaien projecten met die sleutel in een pijplijn, en die mogen niet omvallen. |
| Alle handhaving in één keer naar het beslispunt verhuizen | de gate staat op meer dan veertig plekken; één verkeerd omgezette plek is een 403 in productie of, erger, een verdwenen 403. |

## 3. Fasering

Elke fase heeft op zichzelf waarde en kan apart worden uitgerold. De eerste is met opzet klein en verandert geen gedrag.

### Fase 0: repareer wat aantoonbaar kapot is

Dit hangt niet aan enige keuze en hoort niet op een besluit te wachten.

- `GET /api/subdomains/check/{subdomain}` en `GET /api/v1/backup/status` geven altijd 401 omdat hun handtekening geen `project_name` bevat (inventarisatie, 8.1). Repareer of verwijder ze. Merk op dat `get_backup_status` bij herstel platformbrede waarden (`current_namespace`, `locked_by`) aan elke projectsleutel zou tonen; herstellen is dus niet vanzelf de veilige keuze.
- Vijf voortgangsroutes in de interface renderen een taak zonder te toetsen of de aanroeper lid is van het bijbehorende project (8.2). Voeg de controle toe die de API-tegenhanger al doet.
- `modal_wizard_load_step` mist de back-up-/hersteluitzondering die zijn vijf zusterroutes wel hebben (8.6).
- `_enrich_user_info` schrijft `is_admin`, `is_developer` en `is_manager` uit een tokenclaim waar geen enkele beslissing aan hangt (8.11). Verwijder ze, en met hen de suggestie dat er een rollenmechanisme is. Let op: `operations-manager/python/tests/test_user_service.py` toetst de drie velden in vijf testfuncties (`test_role_flag_admin`, `test_role_flag_administrator_variant_not_recognized`, `test_role_flag_dev`, `test_role_flag_manager_variants`, `test_no_role_flags_when_no_role`). Die vallen om zodra de velden verdwijnen en horen in dezelfde wijziging mee te gaan; dit is dus geen wijziging van één regel.

**Waarde op zichzelf:** vier bevestigde defecten weg, en één misleidend mechanisme minder voor wie hierna aan het ontwerp begint.

### Fase 1: de handelingenlijst als code

Leg de rechtencatalogus uit document 1 vast als een lijst in de code, en koppel elke route eraan. VOORSTEL voor de vorm: een decorator of registratie die per route een `action.name` opgeeft, plus een controle bij het opstarten die faalt als een geregistreerde route geen handeling heeft.

Geen enkele beslissing verandert. Wat verandert is dat de vraag "welke routes hebben geen gate" voor het eerst beantwoordbaar wordt, en dat een nieuwe route niet stilzwijgend zonder rechten kan bestaan.

**Waarde op zichzelf:** de klasse fouten uit 8.5 en 8.6 (dezelfde regel op veertig plekken, vijf keer goed) wordt vindbaar in plaats van onvindbaar. Ook zonder enige vervolgfase.

### Fase 2: één beslispunt, schaduwdraaiend

Bouw één functie die een AuthZEN-verzoek aanneemt (`subject`, `action`, `resource`, `context`) en een AuthZEN-antwoord teruggeeft (`decision`, plus `reason_admin`/`reason_user` in `context`). Laat de bestaande helpers hem aanroepen naast hun eigen oordeel en elk verschil loggen. Niets hangt er nog van af.

Haak hier het logboek aan bij **RC-149**: een autorisatiebeslissing is een gebeurtenistype, geen tweede systeem. Wat een beslissing extra vraagt staat in document 2, paragraaf 8.

**Waarde op zichzelf:** elk gelogd verschil is een gat dat vandaag onzichtbaar is. Ook als de fasering hier zou stoppen, levert dit een lijst afwijkingen op die anders niemand vindt.

### Fase 3: de handhaving omzetten

Per helper, in volgorde van aantal aanroepen, en telkens pas nadat de verschillen op nul staan: `require_project_edit_access` (11 aanroepen), `_require_project_member_access` (15), de drie identieke `_require_admin`-kopieën (10, worden er één), de zes handgeschreven rolcontroles, de vijftien handgeschreven lidmaatschapscontroles. De dertien templateblokken komen als laatste, via `search/action`.

**Waarde op zichzelf:** na elke stap is er één plek minder waar de volgende wijziging vergeten kan worden. De winst is cumulatief en elke stap is los terug te draaien.

### Fase 4: rollen expliciet, en één lijst

Voeg de bundel toe: één plek waar staat welke handelingen `admin` omvat. Beslecht daarna de drie rollenlijsten die nu niet overeenkomen (inventarisatie, 4): het schema kent `admin|owner|member|developer`, de keuzelijst biedt `admin|developer|operator`, en de handhaving onderscheidt alleen `admin`/`owner` van de rest. Dit is een besluit, geen bouwwerk; zie 5.1.

**Waarde op zichzelf:** de interface belooft niet langer een rol die het schema weigert, en wie `member` leest weet wat het betekent.

### Fase 5: tokens

Een tokentabel in de database (VOORSTEL: `api_tokens`) met tokenhash, soort, eigenaar, project, scopes, vervaldatum, intrekkingsmoment en laatst-gebruikt. Verplichte vervaldatum met een platformmaximum. Scopes uit de handelingenlijst van fase 1. Na uitgifte niet meer leesbaar.

De bestaande projectsleutel blijft werken en wordt intern vertaald naar een token met alle scopes en geen vervaldatum. Zie 4.2.

**Waarde op zichzelf:** vanaf hier is er een bewijsmiddel dat intrekbaar is, dat verloopt, dat minder mag dan alles, en waarvan het logboek weet welk het was.

### Fase 6: agents

Twee vormen, beide uit document 2, paragraaf 6: een gedelegeerde agent (kortlevend, versmald tot minder dan de persoon, sterft met diens recht) en een agent als eigen principaal aan een project (eigen rechten, kortlevende tokens uit een langlevende installatie). `via_agent` en `on_behalf_of` gaan mee in de context van elk verzoek.

**Waarde op zichzelf:** de eerste agent die op dit platform iets doet, doet dat niet meer met het volledige gezag van het project, en het logboek weet wie hem stuurde.

### Fase 7: pas als er een reden komt

Externaliseren naar een apart beslispunt, of aansluiten op het NL GOV-profiel. Beide worden pas interessant als er een tweede handhavingspunt buiten OPI komt, of als een externe eis het vraagt. Door fase 2 is dat op dat moment een transportwissel.

**Waarde op zichzelf:** geen, vandaag. Dat is precies waarom hij achteraan staat.

## 4. Migratiepad voor wat er nu staat

Een project dat vandaag draait mag hier niet door omvallen. Per bestaand ding:

### 4.1 De vier rollen

| Nu | Voorstel | Migratie |
|---|---|---|
| `admin` | blijft, met een expliciete bundel | geen wijziging in het bestand |
| `owner` | blijft als alias van `admin`, of verdwijnt, besluit 5.1 | bij verdwijnen: eenmalige omzetting naar `admin`, want de handhaving behandelde ze al identiek, dus niemand verliest of wint iets |
| `member` | blijft als alias van `developer`, of verdwijnt, besluit 5.1 | idem, naar `developer` |
| `developer` | blijft, met een expliciete bundel | geen wijziging |
| `operator` | is geen geldige rol; hij staat alleen in de keuzelijst en het schema weigert hem | verwijderen uit `opi/forms/visualizers/providers.py:205-209`; er kan geen projectbestand bestaan dat hem bevat, want het schema wordt bij elke schrijfactie gecontroleerd |

De omzettingen zijn veilig omdat ze per definitie geen rechten verplaatsen: `admin` en `owner` waren al identiek, `member` en `developer` ook. Wie de enum toch wil verfijnen, doet dat ná de omzetting en als bewuste uitbreiding, niet als bijvangst.

### 4.2 De bestaande projectsleutels

De volgorde uit document 2, paragraaf 5.4, met de nadruk op de valkuil:

1. `config.api-key` blijft geldig; er verandert niets aan het projectbestand.
2. De nieuwe tokenweg komt ernaast te staan.
3. Het handhavingspunt vertaalt de oude sleutel naar een token met **exact de 46 handelingen die hij vandaag opent** en geen enkele meer. Dit is de plek waar een verruiming er ongemerkt bij kan sluipen; de vertaling hoort daarom een expliciete, getoetste lijst te zijn en geen wildcard.
4. Het gebruik van de oude sleutel is vanaf stap 3 meetbaar per project.
5. Intrekken kan per project zodra dat project geen aanroepen meer met de oude sleutel doet. Het veld verdwijnt pas als geen enkel project hem nog gebruikt.

### 4.3 `ADMIN_API_KEY` en `MASTER_API_KEY`

Beide zijn gedeelde, statische geheimen uit de omgeving die niemand identificeren, en beide zijn nergens in `bootstrap/` of `infrastructure/` gezet, zonder waarde antwoorden hun negen routes met 501.

- De zes routes achter `ADMIN_API_KEY` zijn platformonderhoud en horen bij een *mens* met platformbeheerderrechten. Voorstel: ze krijgen een tweede, gelijkwaardige weg via SSO plus platformbeheerder, en de sleutel blijft bestaan tot die weg in gebruik is. Daarna vervalt de sleutel.
- De drie routes achter `MASTER_API_KEY` zijn federatie: OPI-naar-OPI. Daar is geen mens, en een gedeeld geheim is er inhoudelijk minder verkeerd. Voorstel: hij blijft, maar wordt een projectloos token uit dezelfde tabel als fase 5, met een vervaldatum en een intrekkingsmogelijkheid. Let op dat `FEDERATION_PEERS` de sleutel van elke peer in platte tekst in een omgevingsvariabele draagt; dat verandert hier niet vanzelf mee en verdient een eigen beslissing.

Zolang beide sleutels niet zijn ingesteld, verandert er voor een draaiend cluster niets, de routes waren daar al 501.

### 4.4 Het hardgecodeerde adres in `startup.py`

`opi/core/startup.py:470` en `:497` zetten hetzelfde persoonlijke e-mailadres neer als standaard-allowlist én als standaard-platformbeheerder.

Voorstel, en dit is de enige migratie met een echt risico op buitensluiting:

1. Zet het adres eerst in de configuratie van elke omgeving waar het nodig is (`ALLOWED_EMAILS`, `ADMIN_EMAILS`), en controleer per omgeving dat het daar staat.
2. Verwijder daarna de regels uit de broncode.
3. Voeg pas dan een opstartcontrole toe die faalt als er nul platformbeheerders zijn geconfigureerd. Die volgorde is bewust: eerst zorgen dat er altijd één is, dan pas afdwingen dat er één is.

Stap 3 is meer waard dan hij lijkt. Vandaag is "OPI draait zonder platformbeheerder" een toestand die niet kan bestaan omdat er een adres in de code staat; zonder dat adres kan hij wél bestaan, en dan is er niemand die hem kan repareren.

Merk ook op dat het laden van de `users`-tabel in een brede `except Exception` staat met alleen een waarschuwing (inventarisatie, 8.10). Zolang dat zo is, kan een databasestoring de allowlist stil verkleinen, en dat wordt pijnlijker zodra de hardgecodeerde noodingang weg is.

### 4.5 De ontsleutelde omgevingsvariabelen op de detailpagina

Gat 8.3: elk projectlid, ongeacht rol, ziet de ontsleutelde `user-env-vars` van elke deployment, terwijl alle andere gevoelige blokken op diezelfde pagina achter `admin`/`owner` staan.

Dit is een besluit vermomd als bug (zie 5.6). Wat er ook uitkomt: het antwoord hoort in de handler te staan en niet in de template, want vandaag ontsleutelt de handler ze hoe dan ook (`opi/web/router.py:1027`) en is de template het enige wat ze tegenhoudt.

**Let op:** als de uitkomst is dat alleen `admin`/`owner` ze mag zien, is dat een *versmalling* die vandaag werkende werkwijzen kan breken, een developer die zijn eigen applicatieconfiguratie opzoekt, kan dat dan niet meer. Dat is een gedragsverandering die aangekondigd hoort te worden, niet stilletjes doorgevoerd.

## 5. Besluiten die een mens moet nemen

### 5.1 Welke rollen blijven er

- **A.** Vier rollen houden en ze echt betekenis geven (vier niveaus, vier bundels). *Kost:* een rechtenmodel met vier trappen, en de vraag wat `owner` méér is dan `admin` moet beantwoord worden.
- **B.** Twee rollen, `admin` en `developer`, met `owner` en `member` als alias tijdens een overgangsperiode. *Kost:* vrijwel niets; het is wat de handhaving vandaag al doet.
- **C.** Twee rollen nu, met een derde erbij zodra er een concrete vraag ligt.

**Aanbeveling: B, met C als vervolg.** Vier namen die twee dingen betekenen is de gevaarlijkste van de drie, omdat iedere lezer aanneemt dat de enum iets belooft. Twee namen die twee dingen betekenen is eerlijk, en een derde toevoegen is later goedkoop zodra de bundels expliciet zijn.

### 5.2 Waar staan lidmaatschap en rol

- **A.** In het projectbestand, waar ze nu staan.
- **B.** In de database.
- **C.** In het projectbestand, met een afgeleide leesindex in de database.

**Aanbeveling: A.** Het is een projecteigenschap, de commit is de registratie, en de `ProjectStore` blijft het enige leespad. C wordt pas interessant als "in welke projecten zit deze persoon" een veelgestelde vraag wordt; dat is nu niet zo.

**Voorwaarde bij B of C:** de zichtbaarheid die de commit vandaag gratis geeft, verdwijnt. Dan is het logboek uit RC-149 een randvoorwaarde vooraf, niet een verbetering achteraf.

### 5.3 Wie geeft tokens uit

- **A.** Een eigen tokentabel met gehashte opslag.
- **B.** Keycloak, met een client per project en token exchange voor delegatie.
- **C.** Beide: Keycloak voor mensen (zoals nu), eigen tabel voor machines.

**Aanbeveling: C.** Dat is de bestaande situatie voor mensen ongemoeid laten en er precies één ding bij bouwen. B alleen legt intrekking en scopes, juist de eigenschappen die het probleem oplossen, op de zwakste plek.

### 5.4 Wat gebeurt er met de projectsleutel

- **A.** Naast de nieuwe tokens laten bestaan, met een einddatum en een meetbaar afbouwpad.
- **B.** Vervangen zodra de tokens er zijn.
- **C.** Laten zoals hij is; alleen nieuwe integraties gebruiken tokens.

**Aanbeveling: A.** B breekt draaiende pijplijnen. C betekent dat de allesopenende, nooit vervallende sleutel er over vijf jaar nog is, en dat is precies de toestand die dit hele plan wil beëindigen. Een einddatum zonder afbouwpad is echter een wens; het meetbare gebruik uit fase 5 is wat A uitvoerbaar maakt.

### 5.5 Mag een agent meer dan de mens die hem startte

- **A.** Nooit.
- **B.** Een gedelegeerde agent nooit; een agent als eigen principaal wel, mits het *aanzetten* zwaarder bewaakt is dan wat hij vervolgens mag.
- **C.** Per geval regelen.

**Aanbeveling: B.** A verbiedt een legitiem en nuttig patroon (een bouwstraat die images vervangt en verder niets, strikter dan elke mens). C is geen besluit maar het uitstellen ervan, en dit is precies het soort vraag waar uitstel betekent dat het antwoord per ongeluk "ja" wordt.

### 5.6 Wie mag de omgevingsvariabelen van een deployment zien

- **A.** Elk projectlid, zoals nu.
- **B.** Alleen `admin` en `owner`, zoals bij alle andere geheimen op diezelfde pagina.
- **C.** Elk lid, maar met een vermelding in het logboek dat ze zijn opgevraagd.

**Aanbeveling: C.** A is de huidige praktijk en breekt niets; B is consistent maar versmalt zichtbaar wat mensen vandaag doen. C houdt de werkwijze intact en maakt tegelijk waar wat de andere twee niet doen: dat je achteraf kunt zien wie erbij is geweest. Dit besluit hangt aan RC-149 en hoort daar afgestemd te worden.

### 5.7 Waar komt "platformbeheerder" vandaan

- **A.** Zoals nu: omgevingsvariabele plus een adres in de broncode.
- **B.** Een vlag in de `users`-tabel, met de omgevingsvariabele als noodingang.
- **C.** Alleen de omgevingsvariabele.

**Aanbeveling: B.** Het is de enige optie waarin "wie is beheerder" een vraag is die je kunt stellen zonder de broncode en de deployment-configuratie naast elkaar te leggen, en de enige waarin het toekennen ervan een gebeurtenis is die je kunt vastleggen. De noodingang blijft nodig, want anders is een lege beheerderslijst onherstelbaar.

### 5.8 Volgen we het NL GOV-profiel

- **A.** Nu al, inclusief `processing_activity_id`, `algorithm_id` en `traceparent`.
- **B.** De aangenomen Authorization API 1.0 nu, het profiel zodra het vastgesteld is.
- **C.** Geen van beide; een eigen vorm.

**Aanbeveling: B.** Het profiel is een werkversie die zichzelf beschrijft als fork van draft 04; erop vastleggen betekent meebewegen met iets dat zelf nog moet meebewegen. `traceparent` is de uitzondering: die kost niets, sluit aan op RC-149 en hoort er sowieso in. C gooit de enige gratis winst weg.

## 6. De kleinste eerste stap

Als er maar één ding gebouwd wordt: **fase 1, elke route wijst één handeling aan, en een route zonder handeling start niet op.**

Waarom dat en niets anders:

- Het verandert geen enkel gedrag, dus het kan niet iets breken dat vandaag werkt.
- Het hangt aan geen enkel besluit uit paragraaf 5. Welke rollen er ook komen, welk beslispunt er ook komt, welke tokens er ook komen, de handelingenlijst is er hoe dan ook voor nodig.
- Het is de enige stap die de belangrijkste klasse fouten *vindbaar* maakt in plaats van te repareren. De elf gaten uit de inventarisatie zijn gevonden door 158 routes te lezen; na deze stap zijn ze te vinden door een lijst te bekijken.
- Zonder deze stap is AuthZEN een envelop zonder brief: er is geen `action.name` om in te vullen.
- En het is precies het punt waarop dit document zichzelf overbodig maakt. Een inventarisatie veroudert bij de eerstvolgende route; een opstartcontrole niet.

Daarnaast, en los daarvan: de vier bevestigde defecten uit fase 0 horen niet op enig besluit te wachten. Ze zijn klein, ze zijn gemeten, en twee ervan zijn een ontbrekende controle die elders in dezelfde codebase wél staat.

## Verwante documenten

- [rechten-inventarisatie.md](rechten-inventarisatie.md), de gemeten uitgangssituatie en de gaten waar dit plan naar verwijst
- [rechten-modellen-en-tokens.md](rechten-modellen-en-tokens.md), de afwegingen achter elke aanbeveling hierboven
- [form-field-rbac.md](form-field-rbac.md), volgt na fase 4; veronderstelt het model dat hier ontworpen wordt
- [tenant-isolation-followups.md](tenant-isolation-followups.md), [project-file-single-path-consolidation.md](project-file-single-path-consolidation.md)
- RC-149, gebeurtenissen vastleggen en melden; het logboek waar fase 2 op aanhaakt
