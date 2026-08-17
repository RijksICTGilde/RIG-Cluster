# Een tabblad via een haak, in plaats van op zes plekken met de hand

**Status**: geparkeerd, 17 augustus 2026. Geen taak, geen planning. Dit legt de waarneming en de afweging vast zodat ze er nog zijn wanneer het wel aan de beurt is.

## De waarneming

Bijna alles wat een dienst aan de projectpagina toevoegt gaat via haken: `config_editables`, `config_form_section`, `config_component_visualizers`, `config_approvals`, `component_form_notices`, en de gebeurtenissen (`UIEvent.PROJECT_SECTIONS`, `DEPLOYMENT_SECTIONS`, `DEPLOYMENT_STATE`). Een dienst declareert wat hij levert en de pagina hoeft hem niet te kennen.

De TABBLADEN gaan niet zo. Die staan met de hand in een tabel, en een tabblad toevoegen raakt zes plekken:

1. een regel in `PROJECT_TABS` (`opi/web/lotc_switch.py:601`) met label en pad;
2. eventueel een regel in `TABS_MET_VOORWAARDE` — verbergen als hij niets te tonen heeft;
3. eventueel een regel in `TABS_MET_DEPLOYMENT` — draagt hij een deployment in zijn pad;
4. eventueel een regel in `OUDE_TABBLADPADEN` (`opi/web/router.py`) voor de doorverwijzing van de oude vorm;
5. twee route-registraties, in de nieuwe en de oude vorm;
6. een tak in de `{% elif active_tab == ... %}`-ketting in `opi/templates_lotc/bg/project-tabs.html.j2`.

Drie van die zes zijn zijtabellen die per tabblad iets zeggen wat eigenlijk een EIGENSCHAP van dat tabblad is. Dat ze los staan is precies waarom `team` er ooit in de ene tabel wel en in de andere niet stond, met een 500 als gevolg (RC-101); `tests/test_lotc_tabbladen_url.py` loopt sindsdien beide lijsten af, wat een vangnet is voor een vorm die het probleem in stand houdt.

En twee van de negen tabbladen zijn volledig eigendom van één dienst: **Metrics** is de metrics-scraper, **Backups** is backups. Ze zijn allebei ontstaan doordat een dienstblok te groot werd voor de deploymentpagina, en toen met de hand een tabblad kreeg.

Dat het anders kan staat al in de code: `services-info` IS de generieke haaktab, met die motivering erbij — "dit tabblad draagt de blokken die DIENSTEN zelf leveren via detail_page_sections, en dat is de haak en niet het onderwerp".

## Wat de vorm zou zijn

Een dienst declareert zijn tabblad, met de drie zijtabellen als eigenschappen erin: label, volgorde, verbergen-als-leeg, en draagt-een-deployment. De routes en de doorverwijzingen komen uit die declaratie in plaats van uit zes handgeschreven plekken, zoals de config-routes nu al uit de editables komen.

Metrics en Backups verhuizen dan naar hun dienst.

## Wat er eerst beslist moet worden

**Niet elk tabblad is van een dienst.** Overzicht, Team, Componenten, Deployments en Taken zijn van het platform. De haak levert dus dienst-tabbladen NAAST een vaste kern, en niet in plaats daarvan. Een ontwerp dat alles door de haak duwt maakt de eenvoudige gevallen ingewikkelder.

**De volgorde in de balk is een ontwerpkeuze, geen wedstrijd.** Vandaag is de volgorde van `PROJECT_TABS` de volgorde in de balk, en dat is te lezen. Laat je diensten een volgordegetal declareren, dan bepaalt de installatievolgorde straks hoe de pagina eruitziet. Beantwoord dit expliciet; het is de manier waarop zo'n haak in de praktijk lelijk wordt.

**Wat gebeurt er met de oude paden.** `OUDE_TABBLADPADEN` bestaat voor gedeelde links van vóór RC-93. Een gegenereerd tabblad moet zeggen of het een oude vorm heeft, en de meeste zullen die niet hebben (`backups` heeft hem bewust niet, want dat tabblad bestond toen nog niet).

**Wat het waard is.** Dit is een refactor van iets dat werkt. De winst is dat de volgende dienst met een eigen scherm hem op één plek declareert in plaats van zes, plus dat de drie zijtabellen verdwijnen. De prijs is een migratie van negen bestaande tabbladen en een nieuwe indirectie in de belangrijkste pagina van de applicatie. Doe hem pas als er een derde dienst met een eigen tabblad aankomt, of als die zijtabellen opnieuw uit de pas lopen.

## Waar het vandaan komt

Opgemerkt door de eigenaar bij het weghalen van de dubbele metingen: "we hebben natuurlijk nu ineens hardcoded tabs lopen toevoegen, terwijl we de rest via een mooi hook systeem hebben gemaakt". De aanleiding was dat het metingenblok op twee tabbladen stond (weggehaald in `cd557e34`); onder deze vorm zou de metrics-scraper een TABBLAD bezitten en was die dubbeling niet ontstaan.

Zie ook `instructions/services.md` voor de haken die er al zijn, en `features/services-info-tabblad.md` voor de afweging achter het bestaande haaktabblad.
