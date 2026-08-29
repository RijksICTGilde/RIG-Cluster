# Het beheerdeel van ZAD: rollen, overzicht, en wat een beheerder moet weten

**Onderzoeks- en schrijfopdracht, geen bouwopdracht.** De oplevering is markdown. Er wordt
geen productiecode gewijzigd, niets uitgerold en geen migratie toegevoegd. Schemaschetsen en
schermschetsen mogen, maar dan als codeblok of tekstschets IN het document.

**Lees eerst de basis van deze tak.** Deze opdracht bouwt op RC-148 en die drie documenten
staan in de branch waarop je werkt: `plans/meldingen-inventarisatie.md`,
`plans/meldingen-oplossingsrichtingen.md`, `plans/meldingen-plan-van-aanpak.md`. Ze zijn goed
en het datamodel blijft staan. Wat erin ontbreekt is de aanleiding voor deze opdracht.

## De aanleiding, letterlijk

RC-148 zet de standaard voor een platformbeheerder op **"alles, inclusief type 12"**, met
filters op de lijst als enige rem. De opdrachtgever wil dat niet: "ik hoef niet per se voor
alle projecten alles te zien". En hij voegt eraan toe dat het beheerdeel van ZAD sowieso nog
niet lekker is uitgewerkt.

Dat tweede is de eigenlijke opdracht. De firehose in het postvak is een symptoom: als er een
behoorlijk beheerdersoverzicht wás, was de helft van die meldingen daar een regel geweest en
geen postvakrij. Deze opdracht gaat dus over het beheerdeel als geheel, met de meldingen als
de plek waar het gat zichtbaar werd.

## Oplevering

1. `plans/beheer-in-zad-inventarisatie.md` -- wat er vandaag is, gemeten (deel 1).
2. `plans/beheer-in-zad-plan-van-aanpak.md` -- het rollenmodel, het overzicht, de grensregel,
   de fasering en de beslissingen (deel 2, 3 en 6).
3. **Wijzigingen IN `plans/meldingen-plan-van-aanpak.md`**, met bovenaan of onderaan een korte
   lijst van wat je veranderd hebt en waarom (deel 4 en 5). Twee documenten die elkaar
   tegenspreken zijn erger dan één document dat is bijgewerkt. Raak de inventarisatie en de
   oplossingsrichtingen alleen aan als je er een fout in vindt, en meld dat dan apart.

Stijl: Nederlands, in de vorm van de bestaande `plans/`-documenten. Geen em-dashes. Elk punt
op zichzelf leesbaar. Verzin geen namen die als vaststaand overkomen: markeer een zelfbedachte
naam expliciet als voorstel. Verifieer elke bewering over de code tegen de code, met een anker
(`pad/bestand.py:regel`). Wat je niet terugvindt, staat als "bestaat niet" in het document.

## Deel 1: wat er vandaag is

Meet het, schrijf het niet uit het hoofd op.

- **Het beheermenu.** Vijf ingangen (`opi/web/menu.py:65-70`, gegroepeerd in
  `opi/web/navigation_lotc.py:126`): gebruikersbeheer, gebruik en kosten, aanvragen,
  dienstenstatus, metrics-explorer. Per pagina: wat kun je er doen, wat is read-only, en wat
  ontbreekt er waardoor iemand alsnog naar `kubectl` of de Taskfile moet.
- **De rol.** Er is er één, plat: `is_platform_admin`, en die wordt maar op zes plekken
  aangeroepen. Zoek uit waar de allowlist vandaan komt en hoe die zich verhoudt tot de
  `users`-tabel en tot Keycloak. Beschrijf ook wat een platformbeheerder impliciet krijgt zonder
  dat er een controle staat: `is_user_authorized_for_project` geeft hem toegang tot elk project
  en `get_user_role_for_project` geeft hem overal `admin`
  (`opi/services/project_authorization.py`). Dat is een grote bevoegdheid met één vinkje ervoor.
- **Beheerwerk dat buiten het beheerdeel valt.** Dingen die een beheerder doet maar die niet in
  het menu staan: bootstrap-acties, reconciliatie en opruimen van wezen
  (`features/service-orphan-reconciliation.md`, `opi/services/marked_for_deletion_service.py`),
  backups en retentie, de resource-tuner, clusterconfiguratie, de logbewaker naar ntfy. Maak de
  lijst compleet en zeg per stuk waar het vandaag zit: UI, API, CLI, Taskfile of alleen kubectl.
- **Wat een beheerder vandaag NIET kan zien.** Dit is de belangrijkste kolom van deel 1. Welke
  vraag die een beheerder redelijkerwijs stelt ("welke projecten zijn ongezond", "wat wacht er
  op mij", "wat heeft het platform vannacht zelf veranderd") is vandaag niet in één scherm te
  beantwoorden.
- **De schaal.** Hoeveel projecten en hoeveel platformbeheerders zijn er, en hoeveel
  gebeurtenissen per dag zou een platformbeheerder onder de huidige RC-148-standaard krijgen?
  Leid dat af uit wat meetbaar is (aantal projecten in `projects/`, taaksoorten en hun frequentie,
  wat de inventarisatie al vaststelde) en schrijf de methode erbij. **Dit getal draagt het hele
  argument**, dus liever een onderbouwde orde van grootte met de aanname erbij dan een precies
  getal uit de lucht.

## Deel 2: het rollenmodel

De vraag is niet "welke rollen zouden leuk zijn" maar "welke scheiding verdient het om te
bestaan". Weeg minstens deze drie:

- **Eén rol houden zoals nu**, en het probleem oplossen in het overzicht en de meldingen. Neem
  dit serieus als optie: het is de KISS-uitkomst en hij kan winnen.
- **Een leesrol naast de beheerrol** (meekijken zonder kunnen ingrijpen), voor wie wil weten hoe
  het platform ervoor staat zonder de sleutel tot elk project te krijgen.
- **Een taakgerichte scheiding**, bijvoorbeeld wie aanvragen mag afhandelen versus wie
  gebruikers beheert.

Betrek de BIO erbij voor zover die hier echt iets zegt (skill `bio`: minimale bevoegdheden,
functiescheiding, en wat er over beheerderstoegang geëist wordt op ons BBN-niveau). Kort en
toepasbaar, geen compliance-opstel: welke eis dwingt hier iets af, en welke niet.

Beantwoord expliciet: **hoort een beheerder standaard bij alle projecten te kunnen, of hoort
daar een handeling voor te staan die zichtbaar is?** Dat is een beveiligingsvraag en een
meldingsvraag tegelijk, want wie overal bij kan, is ook overal belanghebbende.

## Deel 3: de beheerdersstartpagina en de grensregel

**De grensregel is het hart van deze opdracht.** Formuleer een regel die per gebeurtenis
beslist waar hij thuishoort, en maak hem toetsbaar, dus zo dat twee mensen er onafhankelijk
dezelfde uitkomst mee krijgen. De drie bestemmingen:

1. **Een persoonlijk postvak.** Iets wat aan JOU gericht is en waar jij iets mee moet.
2. **Een beheerdersoverzicht.** Een toestand van het platform die je bekijkt wanneer je kijkt,
   en die geen postvakrij per beheerder verdient.
3. **Een opskanaal** (ntfy, Grafana, de logbewaker). Iets voor wie dienst heeft, niet voor wie
   beheerder is.

Loop daarna de twaalf meldingstypen uit `plans/meldingen-inventarisatie.md` erlangs en wijs ze
toe met die regel. Waar het schuurt, is de regel niet goed genoeg.

Schets vervolgens de startpagina: wat staat erop, in welke volgorde, en wat staat er
nadrukkelijk NIET op. Denk aan wat wacht op een beslissing, wat kapot is, wat het platform
zelf heeft veranderd zonder dat iemand erom vroeg, en wat er aan komt (verlopende dingen).
Houd je aan de componentenbouwlijn (`ROOS_CLAUDE_REFERENCE.md` in
`/Users/robbertuittenbroek/IdeaProjects/jinja-roos-components/`, en `features/lotc-bouwlijn.md`).
Een tekstschets volstaat.

Zeg er ook bij hoe die pagina zich verhoudt tot wat er al is: de dienstenstatus, de
gebruikspagina en de aanvragenpagina bestaan. Wordt de startpagina de nieuwe ingang met die
drie eronder, of komt er een zesde pagina bij? Een zesde pagina bij een beheerdeel dat al niet
lekker is, is een antwoord dat je moet verdedigen.

## Deel 4: de correctie op de meldingen voor beheerders

Vervang "platformbeheerder: alles" door iets verdedigbaars. Weeg minstens deze vier
mechanismen, en neem ze niet allemaal over als er twee volstaan:

1. **Aan mij gericht versus platformbreed.** De kolom `reason` draagt `platform-admin` al als
   aparte waarde naast `approver` en `actor`, dus het onderscheid is in de data aanwezig en
   wordt alleen niet gebruikt. Het goedkoopste antwoord is misschien: wat `reason` gelijk aan
   `platform-admin` heeft, gaat naar het overzicht en niet naar het postvak. Toets dat.
2. **Per project volgen of dempen.** Het GitHub-model waar de wens naar wijst heeft dit als kern
   (watch, participating, ignore per repo) en RC-148 heeft het niet overgenomen. Beoordeel of het
   erbij hoort, wat het aan model kost (een tabel erbij, of een kolom), en of het voor gewone
   projectleden ook iets oplost of alleen voor beheerders.
3. **Een drempel op ernst.** De kolom `severity` bestaat (informational, actionable, outage) en
   wordt nergens gebruikt om te bepalen wat iemand standaard krijgt.
4. **Escalatie als niemand kijkt.** Een aanvraag die drie dagen ligt. Zeg ook eerlijk of dit in
   fase 1 hoort of dat het een latere zorg is.

Lever als resultaat een **nieuwe standaardentabel** voor de platformbeheerder in
`plans/meldingen-plan-van-aanpak.md` (het blok "De standaarden per rol"), plus de
gevolgen elders in dat document: het voorkeurenscherm, de "waarom kreeg ik dit"-regel, en de
lijst van wat niet uitgezet mag kunnen worden. Als jouw voorstel het datamodel raakt, werk dat
bij in `plans/meldingen-oplossingsrichtingen.md` en meld het.

## Deel 5: de live-vraag

RC-148 zet websocket tegenover peilen en slaat de middenweg over. Weeg **server-sent events**
alsnog, eventueel met `LISTEN/NOTIFY` van Postgres eronder, en beslis **per plek** in plaats
van in het algemeen:

- de teller in de kop (op elke pagina van elke gebruiker in elk tabblad);
- de postvakpagina (één pagina die iemand bewust openzet);
- het beheerdersoverzicht uit deel 3 (denk aan een muurscherm dat de hele dag openstaat).

Neem in de weging mee: hoeveel processen OPI draait en of de boekhouding per proces een
probleem is (het bezwaar dat RC-148 tegen de websocket maakt, geldt deels ook hier, en dat
"deels" is precies wat uitgezocht moet worden), wat een langlopende verbinding doet op ODCN
(route- en proxytijdslimieten), wat de CSP toestaat, en wat er gebeurt bij een herstart van
OPI. Meet wat te meten valt en markeer de rest als aanname.

De uitkomst mag "peilen blijft, ook hier" zijn. Dan staat er tenminste waarom, en dan is het
een beslissing in plaats van een overgeslagen alternatief. Werk de uitkomst bij in de paragraaf
"De verversingsweg" van `plans/meldingen-plan-van-aanpak.md`.

## Deel 6: fasering en beslissingen

Zeg expliciet **wat er aan fase 1 van RC-148 verandert.** Blijft die fase goedkeuringen als
eerste bron doen, of komt het beheerdersoverzicht ervoor of ernaast? Als het overzicht eerst
moet, zeg dat dan, ook als het de meldingen vertraagt.

Sluit af met de openstaande beslissingen, elk met jouw aanbeveling erbij zodat er ja of nee op
te zeggen is, en met een expliciete niet-doen-lijst.

## Randvoorwaarden

- Geen productiecode, geen migratie, geen uitrol, niets in de sandbox of op productie.
- Verzin geen ontbrekende functionaliteit erbij. Als iets er niet is, is dat een bevinding.
- Corrigeer RC-148 waar het aantoonbaar fout is, maar herschrijf het niet omdat het anders kan.
  Elke wijziging in die documenten staat in de wijzigingslijst met de reden erbij.
- De opdrachtgever is de bouwer van dit platform en leest mee op detail. Getallen worden
  nagemeten, dus schrijf er de meetmethode bij.

## Klaar als

1. De inventarisatie beschrijft de vijf beheerpagina's, de rol en zijn zes aanroeppunten, het
   beheerwerk dat buiten het menu valt, en de vragen die een beheerder vandaag niet in één
   scherm beantwoord krijgt. Elke bewering heeft een anker of staat als "bestaat niet".
2. Er staat een onderbouwde schatting van het aantal meldingen per dag voor een
   platformbeheerder onder de huidige RC-148-standaard, met de methode erbij.
3. Er staan minstens drie rollenmodellen tegen elkaar, met één aanbeveling, en het antwoord op
   de vraag of een beheerder standaard bij elk project hoort te kunnen.
4. De grensregel staat er, is toetsbaar geformuleerd, en is toegepast op alle twaalf
   meldingstypen.
5. Er staat een schets van de beheerdersstartpagina, inclusief wat er niet op staat en hoe hij
   zich verhoudt tot de bestaande vijf pagina's.
6. `plans/meldingen-plan-van-aanpak.md` bevat een nieuwe standaardentabel voor de
   platformbeheerder en een bijgewerkte paragraaf over de verversingsweg, met een wijzigingslijst.
7. Er staat wat er aan fase 1 verandert, plus de beslissingen met aanbeveling.
8. De PR bevat alleen markdown in `plans/`.
