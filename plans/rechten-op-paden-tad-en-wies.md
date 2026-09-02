# Rechten op paden: wat we uit tad en wies overnemen

Status: **werkdocument, 2 september 2026.** Vastgelegd uit een gesprek, niet uitgewerkt en niet besloten. Het vult twee bestaande documenten aan en herziet er een paar beslissingen uit; zie de laatste twee paragrafen.

Verwant: `features/futures/rechten-plan-van-aanpak.md` (de aanbeveling van RC-150), `features/futures/rechten-inventarisatie.md` (de meting), `features/futures/rechten-modellen-en-tokens.md` (de afwegingen), `plans/otp-en-verhoogde-rechten.md` (2 augustus, verhoogde rechten en OTP), `features/futures/form-field-rbac.md` (het al benoemde gat op veldniveau).

Alle zelfbedachte namen hieronder zijn **VOORSTEL**.

## Aanleiding

RC-150 beveelt een handelingenlijst aan plus één beslispunt dat de AuthZEN-verzoekvorm spreekt maar in het OPI-proces draait. Wat dat plan openlaat is hoe een resource-identificatie eruitziet en hoe een regel geschreven en opgelost wordt. Twee bestaande systemen in eigen huis beantwoorden elk een van die twee, en dit document legt vast wat we daaruit overnemen.

## De twee referentiesystemen, gemeten

### tad (`/Users/robbertuittenbroek/IdeaProjects/tad`)

Data-gedreven en padgebaseerd. `Rule(role_id, resource, verbs)` staat in de database, waarbij `resource` een padsjabloon is (`organization/{organization_id}/algorithm`, in `amt/core/authorization.py`) en `verbs` uit `List, Read, Create, Update, Delete` komt. Een `Authorization` koppelt een gebruiker aan een rol binnen een scope (organisatie X, algoritme Y). Bij elk verzoek expandeert `AuthorizationsService.find_by_user` alles tot één platte kaart `{"organization/42/algorithm": [Read, List]}` op `request.state.permissions`. De annotatie `@permission({RESOURCE: [VERB]})` (`amt/api/decorators.py`) vult zijn eigen sjabloon met de route-kwargs en doet een exacte sleutelopzoeking.

Sterk: rollen en regels zijn data, dus wijzigen vraagt geen uitrol; het pad draagt de scope; en de hele rechtenset van de aanroeper zit in één dict, waardoor het renderen van knoppen geen extra vraag kost.

Zwak: exacte sleutelvergelijking, dus geen hiërarchie en geen overerving; een slug-naar-id-opzoeking in de database gebeurt *binnen* de autorisatiedecorator; alleen routeniveau; en geen voorwaarden buiten identiteit van de instantie.

### wies (`/Users/robbertuittenbroek/IdeaProjects/wies`)

Code-gedreven en relationeel. Een registry van predicaten: `@rule(verb, target)` waarbij target een modelklasse is (heel object) of een Editable-instantie (veld), in `wies/core/permission_engine.py`. `has_permission(verb, obj, user, field=None)` zoekt op `(verb, model, veldnaam)` en valt terug op `(verb, model, None)`. Regels zijn gewone functies over `(user, obj)` die elkaar aanroepen: `update_service` delegeert naar de bovenliggende assignment. Alle regels staan in één leesbaar bestand, `wies/core/permissions.py`.

Sterk: relationele voorwaarden zijn één functie (`_is_placed_on_service`); veldregels met een nette terugval; meest-specifieke-wint; en de regels zijn van boven naar beneden te lezen als de rechtenmatrix.

Zwak: regels zijn code, dus wijzigen vraagt een uitrol; geen data-gedreven rollen; niet als één kaart aan de UI te geven; en de voorwaarden zijn ondoorzichtig voor een extern beslispunt.

## Het model

Werkwoorden op een object, waarbij type versus instantie geen apart kenmerk is maar padiepte. `project/` is de verzameling, `project/{p}` is er één, `project/{p}/deployment/{d}` is er één binnen één. Daarom heeft tad `List` en `Read` als aparte werkwoorden: List hoort bij de verzameling, Read bij de instantie.

VOORSTEL voor de objecten in ZAD: `platform`, `project/`, `project/{p}`, `project/{p}/member`, `project/{p}/deployment/{d}`, `project/{p}/component/{c}`, `project/{p}/service/{s}`, `project/{p}/secret`, `project/{p}/backup/{b}`, `project/{p}/token/{t}`.

VOORSTEL voor de werkwoorden: CRUDList als basis, plus de werkwoorden die ZAD werkelijk heeft en die geen `Update` zijn. Uitrollen, verversen, slapen, wekken, klonen, herstellen en roteren komen rechtstreeks uit de 23 tasktypes in `opi/core/async_task_service.py`. Dat is een bewuste uitbreiding op CRUDList en moet als zodanig opgeschreven worden.

De matrix wordt dun, en die dunheid is informatie: `Clone` bestaat alleen op database en bucket, `Wake` alleen op een deployment. Een vol vak is verdacht.

## Twee dingen die anders misgaan

### De cellen zijn regels, geen vinkjes

Vul je de matrix met "welke rol krijgt hier een vinkje", dan bouw je het huidige probleem opnieuw maar groter: vier rollen die twee niveaus zijn, uitgesmeerd over tweehonderd vakjes. Een cel bevat een regel, en de meeste cellen horen leeg te blijven omdat de algemene regel op `project/{p}` dekt wat eronder hangt. Je vult alleen in waar het afwijkt.

Daar hangt de derde as aan. Werkwoord en object zijn er twee; de regel moet iets over **wie** zeggen, en dat is in ZAD niet alleen een rol: rol-op-dit-project, platformrol, organisatielidmaatschap, of de aanroeper een token met scopes is, en of het een agent is die namens iemand handelt. Leg die vorm vooraf vast, anders wordt het ongestructureerde code in de cel. Vastgelegd is het het `subject` uit de AuthZEN-vorm, en dan past de tijdelijke verhoging hieronder er zonder extra machinerie in.

### De matrix wordt afgeleid, niet bijgehouden

Begin je met de matrix opschrijven en zoek je daarna uit waar je hem toepast, dan loopt hij bij de eerstvolgende route uit de pas. Dat is precies wat `rechten-inventarisatie.md` over zichzelf zegt: een momentopname, geen mechanisme.

De volgorde is andersom. Eerst wijst elke route zijn handeling aan en start de applicatie niet op als er een zonder zit; dat is fase 1 uit `rechten-plan-van-aanpak.md` en het hangt aan geen enkel openstaand besluit. Daarna is de matrix een uitdraai van de code, en is "welke routes hebben geen regel" voor het eerst beantwoordbaar.

## Waar je het toepast: vier plekken, niet uitwisselbaar

1. De poort op de route. Dit is waar tad zit.
2. Het veld of de Editable. Dit is waar wies zit, en waar `form-field-rbac.md` al om vraagt.
3. De template, dus welke knop je toont. Moet uit dezelfde regel volgen als 1 en 2, anders lopen ze uit elkaar; de inventarisatie telde dertien rolcontroles in templates naast die in de handlers.
4. **De query, dus welke rijen een lijst teruggeeft.** Deze wordt standaard vergeten en hier zitten de lekken, want een lijstendpoint dat niet of te laat filtert komt door alle drie de andere poorten heen. tad vangt het op door List een werkwoord op de verzameling te maken; wies laat het juist buiten de engine. ZAD moet hier een expliciet antwoord geven, want "diensten die je alleen mag zien als je voor organisatie X werkt" is een lijstvraag.

## Wat we waaruit overnemen

**Uit tad: de annotatie en de padvorm.** ZAD's objecten zijn echt hiërarchisch, dus een pad draagt de scope waar een vlakke handelingsnaam dat niet doet. Dit is een aanscherping van `rechten-plan-van-aanpak.md`, waar de handeling `<object>.<werkwoord>` heet en het object apart mee moet.

**Niet uit tad: de unie met exacte sleutelvergelijking**, want dan is een specifieker pad een extra recht en kan toevoegen per definitie nooit iets inperken. En niet de databaseopzoeking binnen de decorator; de ZAD-variant van die verleiding is een ProjectStore-aanroep in de poort, en dat mag één opzoeking zijn die de beslissing in gaat, geen uitwaaiering.

**Uit wies: hoe een regel geschreven en opgelost wordt.** De predicaatregistry, meest-specifieke-wint, en regels die de bovenliggende regel aanroepen in plaats van de voorwaarde te herhalen. Meest-specifieke-wint is de mechaniek waarmee specifieker toevoegen ook echt strikter maakt; `update_user_email` in wies is er het uitgewerkte voorbeeld van, strenger dan de objectregel eronder.

Let op dat die mechaniek twee kanten op snijdt: een specifieke regel kan ook ruimer zijn dan de algemene. Wies gebruikt dat bewust. Willen we dat niet, dan moet de richting per regel expliciet zijn.

**De splitsing die geen van beide expliciet maakt en die ZAD nodig heeft: beleid is code, grants zijn data.** Wat de rol `developer` betekent is een ontwerpbesluit dat review verdient, dus dat hoort in één leesbaar bestand zoals bij wies. Wie welke rol houdt op welk project is data, en dat staat in ZAD al in het projectbestand, bewust, om GitOps-redenen. tad zet allebei in de database, wies allebei in code.

## Verhoogde rechten als grant met een vervaltijd

Een verhoging is een grant met een `valid_until`. Geen tweede mechanisme, geen vlag die elke poort apart raadpleegt, en "een combinatie van rulesets" is de unie die er toch al is, nu met een tijddimensie.

Het beste gevolg: `is_platform_admin()` verdwijnt als boolean. Platformbeheerder wordt een rol die je permanent of tijdelijk houdt. Dat is een van de vier losse gezagsbegrippen uit de inventarisatie weg, en het is "geen superadmin maar een extra rechtenset".

Twee eisen die erbij horen. Een verhoging mag alleen optellen en nooit inperken, anders maakt meest-specifieke-wint van een tijdelijke gunst een tijdelijk verlies en dat is niet te debuggen. En een verhoging draagt wie hem gaf, waarom en tot wanneer, met een gebeurtenis bij activeren en bij verlopen; dat haakt op het gebeurtenissenspoor (RC-149 en RC-163).

### Dit herziet drie genomen beslissingen

`plans/otp-en-verhoogde-rechten.md` besloot op 2 augustus dat verhoging een **bevestiging** is van rechten die je al hebt, met als expliciete regel "de rol bepaalt nog steeds óf je iets mag; de verhoogde stap bepaalt alleen dat je het nu bewust doet", en met een expliciet verbod op een tweede rollenstelsel. Wat hier staat is een tijdelijke **toekenning**, en dat is iets anders.

De grantvorm respecteert dat verbod beter dan het oorspronkelijke ontwerp, want hij hergebruikt het gewone mechanisme in plaats van een verhoogde toestand naast de rol te zetten. OTP wordt dan de activeringsvoorwaarde van de grant in plaats van een eigen as. Beslissing 8.5, het venster van vijf minuten dat niet meeschuift, blijft overeind als standaardlooptijd van zo'n grant.

De herziening raakt 8.5, 8.6 en 8.7 en moet expliciet worden vastgelegd, niet stilzwijgend.

## Veldniveau: wat er al is, en wat de toets is

ZAD heeft veldniveau al, twee keer, en het heet geen van beide zo.

**`ApprovalSpec`** (`opi/services/catalog/approval.py:94`): een dienst declareert dat een waarde die hij beheert goedkeuring nodig heeft, met een `ApproverScope` erbij, en met een definitie, een check, een lijst, een vastlegging en een melding aan de aanvrager. Eigen domeinen lopen hier al doorheen.

**`ctx.user_role`** op de dienstencontext (`opi/services/catalog/base.py:570`, met de docstring "lets a service gate on the viewer's role"). Drie diensten gebruiken dat met de hand, alle drie met hetzelfde `("admin", "owner")` erin geschreven: `invite/__init__.py:235`, `attachments/__init__.py:267`, `keycloak/__init__.py:99`.

En één plek waar niets besloten is: `rechten-inventarisatie.md` stelt vast dat een `developer` of `member` de door de gebruiker gezette geheimen van elke deployment van het project leest. Een leesrecht op een veld dat nooit een beslissing was.

### De driedeling

1. Mag je het zien. Lekt zodra je het alleen in de template afdwingt.
2. Mag je het zetten. Is decoratie zodra je het alleen bij het renderen afdwingt, want de POST komt gewoon binnen.
3. Mag deze waarde gelden. Iedereen mag voorstellen, een ander beslist.

De klassieke fout is 2 gebruiken waar 3 bedoeld is. "Een developer mag dit veld niet zetten" levert een doodlopende weg op; "een developer mag het aanvragen, een platformbeheerder beslist" levert een werkstroom op. ZAD koos voor domeinen al goedkeuring, en dat hoort de standaard te zijn voor alles waarvan het gevolg het project verlaat.

### De toets

Verlaat het gevolg het project? Zo ja, kandidaat voor een eigen regel. Zo nee, dekt de projectrol het al.

Verlaat het project: een eigen domein, resourcegrenzen (gedeelde clustercapaciteit), het cluster waarop iets draait, registry-credentials, netwerktoegang en `restrict-access`, iets publiek bereikbaar maken, sleutels en tokens, en de rollen van leden zelf. Blijft binnen: een componentnaam, de waarde van een env-var, replicas binnen het quotum.

Veldrechten horen zeldzaam te zijn.

### Aanbeveling: begin bij de dienst, niet bij het veld

Dat dekt "mag je deze dienst gebruiken" volledig, inclusief de organisatievariant. Het aantal regels is klein, want de meeste diensten zeggen niets. En het heeft een plek die de architectuur al voorschrijft: een dienst bezit zijn config, formulieren, manifesten en haken, dus hij hoort ook zijn toegangsregel te bezitten, als één declaratie in het dienstpakket in plaats van een regel in een centraal bestand. Meteen ruimt het de drie handgeschreven rolcontroles op.

Het is geen omweg: het is dezelfde mechaniek die veldniveau nodig heeft, op een grover object.

### Als veldniveau er komt, twee harde eisen

Bind de regel aan de Editable en niet aan een veldnaam als string, zoals wies doet, want dan valt de regel luid om bij hernoemen in plaats van stil nooit meer te matchen. En dwing af op het schrijfpad, waarbij het renderen uit diezelfde regel volgt.

En een derde die hier specifiek bijt: een veldregel die niemand kan halen is onzichtbaar. Dit project heeft daar een geschiedenis mee, met stille drops in wizard-modals, een schemapoort die herverwerking weigerde zonder dat iemand het zag, en dp-bn7 dat wekenlang alle deploys blokkeerde op een geslikte validatiefout. Een veldregel moet zichtbaar falen, met de reden en met wie het wel zou mogen.

## Het punt dat anders een vijfde gezagsbegrip oplevert

`ApprovalSpec` en het rechtenmodel moeten één ding worden. `ApproverScope` heeft nu eigen namen (`platform-admin`, `project-admin`, `project-member`) die niet overeenkomen met de vier projectrollen. Bouw je veldrechten zonder die samen te voegen, dan staan er vijf gezagsbegrippen naast elkaar in plaats van vier, en dat is precies wat het plan wil opruimen.

## Open beslissingen

1. Padvorm als resource-identificatie in plaats van `<object>.<werkwoord>`: overnemen of niet. Raakt fase 1 van `rechten-plan-van-aanpak.md`.
2. Mag een specifiekere regel ook ruimer zijn dan de algemene, zoals bij wies, of alleen strikter.
3. Wordt verhoging een grant met vervaltijd (dit document) of blijft het een bevestiging (2 augustus). Raakt beslissing 8.5, 8.6 en 8.7.
4. Wat is het antwoord op de lijstvraag, dus hoe filtert een verzameling op wat de aanroeper mag zien.
5. Smelt `ApproverScope` samen met de projectrollen, en zo ja hoe.
6. Komt er veldniveau, of blijft het bij dienstniveau plus goedkeuring.

## Volgende stap

Dit uitwerken tot een aanvulling op `features/futures/rechten-plan-van-aanpak.md` en `plans/otp-en-verhoogde-rechten.md`, met de padvorm, de regelresolutie, het grantmodel en de dienst-voor-veld-volgorde concreet uitgeschreven, en met de herziening van 8.5 tot 8.7 expliciet benoemd. Nog niet geshipt.
