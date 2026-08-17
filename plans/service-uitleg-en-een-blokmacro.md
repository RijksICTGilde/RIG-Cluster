# Uitleg bij elke service, en het serviceblok uit één macro

Status: plan, 5 augustus 2026. Niet gebouwd. Aanleiding: een gebruiker die een service aanvinkt krijgt één zin te zien en moet daarop een keuze baseren. Bij de authorization wall staat er een vraagteken naast dat een uitleg opent; bij de andere negentien niet.

## De machinerie bestaat al, de inhoud niet

Dit is bijna geen codewerk. Alles wat nodig is om uitleg te tonen staat er:

- `ServiceDefinition.help_template` (`opi/services/services.py:214`)
- Het vraagteken-icoon op de servicekaart, dat `openServiceHelp()` aanroept (`opi/templates/widgets/service_cards.html.j2`, regel 20 tot 23), alleen gerenderd als `help_template` gezet is
- De modal zelf, op twee plekken opgehangen: `opi/templates/wizard/wizard_page.html.j2` en `opi/templates/project-details/modals.html.j2`
- De templates zelf in `opi/templates/help/`

Gemeten op 5 augustus: **van de twintig services heeft er één een `help_template`**, de authorization wall. De andere negentien hebben `None` en tonen daarom geen icoon. Er ontbreekt dus geen mechanisme, alleen inhoud.

Dezelfde haak bestaat op veldniveau (`Editable.help_template`, met `opi/templates/help/container-image.html.j2` en `resources.html.j2` als enige twee gebruikers). Die valt **buiten** dit plan; hij wordt hier alleen bruikbaarder gemaakt doordat de vorm vastligt.

## Vier stappen

### 1. De Keycloak-teksten gelijktrekken

Er lopen drie omschrijvingen uit elkaar, en ze beschrijven alle drie iets anders dan wat de service doet:

| Waar | Nu |
|---|---|
| `opi/services/services.py:560` | "Configureerbare Keycloak authenticatie met ondersteuning voor SSO en lokale gebruikers" |
| `opi/services/catalog/keycloak/__init__.py:106` | "SSO en authenticatie-instellingen" |
| `opi/forms/i18n.py:214` | "Integreer met de Rijksoverheid SSO voor veilige authenticatie" |

Die laatste noemt alleen SSO, de middelste is nietszeggend. Ze moeten alle drie hetzelfde zeggen: **SSO Rijk en lokale Keycloak-accounts**. Verifiëren: geen overgebleven tekst noemt nog uitsluitend SSO of uitsluitend lokale gebruikers.

Let op: dit is tekst die vandaag in productie zichtbaar is, dus dit is de enige stap met direct zichtbaar effect voor gebruikers.

### 2. Het serviceblok uit één macro

Twee sjablonen bouwen nu een servicekaart, en ze verschillen:

- `opi/templates/widgets/service_cards.html.j2` heeft wél de help-knop, maar staat alleen in het formulier
- `opi/templates/services-overview.html.j2` heeft geen help-knop, terwijl dat juist het overzicht is waar je services vergelijkt

Maak er één macro van (icoon, naam, omschrijving, help-knop) en laat beide sjablonen die gebruiken. Daarmee krijgt het overzicht de knop er meteen bij, wat het punt van dit plan is: je wilt de uitleg zien in het overzicht én bij het configureren.

Verifiëren: een test die eist dat beide sjablonen de macro aanroepen en zelf geen service-icoon meer renderen. Anders groeien ze binnen een maand weer uit elkaar, want dat is precies wat er tot nu toe gebeurd is.

### 3. Negentien uitleg-templates

`opi/templates/help/authorization-wall.html.j2` is de vorm, en die is goed. Kop met icoon, dan:

- Eén alinea **wat is het**, in gewone taal, geen jargon
- **Wanneer gebruik je dit?** als lijstje met herkenbare situaties
- **Wat wordt er ingesteld?** met wat er technisch gebeurt en welke andere services meekomen

Voor elke service een bestand in `opi/templates/help/<service>.html.j2` plus `help_template=` op de `ServiceDefinition`. Gebruik het icoon en de kleur die de service al heeft, zodat de modal en de kaart bij elkaar horen.

De twintig services, met hun huidige omschrijving als startpunt:

| Service | Naam |
|---|---|
| `publish-on-web` | Publiceren op het web |
| `keycloak` | Keycloak Authentication |
| `persistent-storage` | Permanente opslag |
| `temp-storage` | Tijdelijke schijfruimte |
| `postgresql-database` | PostgreSQL Database |
| `namespace-postgresql-database` | Namespace PostgreSQL Database |
| `minio-storage` | MinIO Object Storage |
| `redis` | Redis Cache |
| `namespace-redis` | Namespace Redis Cache |
| `platform` | Platform |
| `attachments` | Bijlagen |
| `authorization-wall` | Authorization Wall (klaar, dient als voorbeeld) |
| `metrics-scraper` | Prometheus Metrics Scraper |
| `sleep-mode` | Slaapstand |
| `invite` | Uitnodiging (zie waarschuwing hieronder) |
| `resource-tuning` | Resource tuning |
| `cross-domain-access` | Cross-domain toegang |
| `user-env-vars` | Eigen omgevingsvariabelen |
| `aliases` | Aliassen |
| `health-check` | Health check |

Vier daarvan zijn systeemdiensten (`platform`, `resource-tuning`, `user-env-vars`, `aliases`). Die kan een gebruiker niet aanvinken, dus ze hebben geen "wanneer gebruik je dit". Ze staan wél in het overzicht, en juist daar is uitleg nuttig: leg uit dat ze altijd draaien en wat ze voor je doen zonder dat je iets kiest.

Schrijf voor de paren die op elkaar lijken (`redis` en `namespace-redis`, `postgresql-database` en `namespace-postgresql-database`, `persistent-storage` en `temp-storage`) expliciet op waarin ze verschillen en wanneer je welke kiest. Dat is de vraag waar een gebruiker op vastloopt, en het is precies wat één zin niet kan zeggen.

Verifiëren: een test die eist dat élke service een `help_template` heeft en dat dat bestand ook echt bestaat. Dit faalt namelijk stil, net als een niet-bestaand icoon: er komt geen fout, de knop verschijnt alleen niet. Zie `tests/test_menu_icons_exist.py` voor dezelfde soort test.

### 4. De teksten laten nakijken

De inhoud wordt geschreven op basis van wat de code doet, maar dit is de tekst die een gebruiker leest bij het maken van een keuze. Lever ze op als één te overziene set zodat ze in één keer nagelopen kunnen worden, en houd ze kort: liever acht regels die kloppen dan dertig die niemand leest.

## Waarschuwing: invite verandert gelijktijdig

De `invite`-service wordt op dit moment aangepast op `branches-samenvoegen-naar-main`. Schrijf de uitleg voor invite als laatste en lees op dat moment opnieuw wat de service doet, in plaats van af te gaan op de omschrijving in de tabel hierboven. Anders staat er een uitleg die bij het samenvoegen al niet meer klopt.

## Volgorde

Stap 2 eerst, want daarna verschijnt elke nieuwe uitleg meteen op beide plekken en zie je bij het schrijven wat je doet. Dan stap 1 (klein, losstaand), dan stap 3, en stap 4 loopt mee.
