# Een eigen afzenderadres op aanvraag

**Status**: ontwerp, niets gebouwd.

**Meetbasis**: commit `06f3c3edd` van 22 september 2026. Alle veldnamen, sleutels, labels en foutteksten hieronder zijn een VOORSTEL, tenzij er een codeanker bij staat.

## Wat het is

Een project kiest vandaag zijn afzenderNAAM en niet zijn afzenderADRES. Dit document beschrijft wat het kost om dat adres aanvraagbaar te maken langs dezelfde weg als een domein, met een goedkeuring door een platformbeheerder, en waarom de bouw daarvan het kleinste deel van de vraag is.

De aanleiding is een terugkerende verwarring die het waard is om apart te noemen: `SMTP_FROM` heet "afzenderadres dat de relay afdwingt" en wordt gelezen als "de naam die de ontvanger ziet". Dat is het niet. `SMTP_FROM` is het volledige adres, de weergavenaam staat in `from-name` bij de dienst, en geen van beide is door de applicatie te veranderen.

## Wat er vandaag staat, gemeten

**Het adres wordt afgeleid, niet ingesteld.** De relay knipt het voorvoegsel `project-` van de geauthenticeerde accountnaam en plakt wat overblijft in het plusdeel: `session.mail rewrite` in `infrastructure/bootstrap/infrastructure/mail/controller/base/config.toml`. OPI rekent hetzelfde adres uit in `generate_mail_sender_address` (`opi/utils/naming.py:743`) en geeft het als `SMTP_FROM` mee, puur als mededeling aan de ontwikkelaar. Beide kanten bouwen op hetzelfde label uit `mail_project_label`, en `MAIL_PROJECT_LABEL_MAX_LENGTH` bestaat precies zodat ze niet uiteen kunnen lopen bij een lange projectnaam.

**De weergavenaam wordt wel ingeladen.** Die valt nergens uit af te leiden, dus OPI genereert er een sieve-script voor (`zad-afzenders`) en schrijft dat via de management-API weg, met een sleutel per account onder `zad.afzender.naam.` (`opi/connectors/mail.py:63`). Een opzoektabel in het geheugen kon niet: die wordt maar een keer gebouwd en een reload ververst hem niet, gemeten op 20 augustus 2026 tegen v0.11.8.

**Het mechanisme voor een AFWIJKEND adres bestaat al en heeft nul gebruikers.** RC-159 zette een tweede sleutelreeks naast de namen, `zad.afzender.adres.<account>` (`opi/connectors/mail.py:83`). Een account met zo'n sleutel krijgt in hetzelfde script een eigen `From:`-adres, terwijl de envelope de afgeleide waarde houdt. De enige klant was het Keycloak-account, en RC-175 heeft dat teruggedraaid: inlogpost vertrekt sinds die wijziging onder het kale basisadres van het cluster en onderscheidt zich alleen nog door `MAIL_KEYCLOAK_FROM_NAME` (`opi/core/cluster_config.py:901`, `opi/manager/mail_manager.py:436`). De reeks staat er dus nog, met een allowlist eronder (`MAIL_SENDER_DOMAIN_ALLOWLIST = frozenset({"rijksoverheid.nl"})`) en met de expliciete opmerking dat niets anders dan het platform hem bedient.

**Let op**: de docstring bij `MAIL_SENDER_ADDRESS_PREFIX` zegt nog "Today that is ZAD's Keycloak account" en is daarmee verouderd. Wie dit oppakt kan die regel meteen rechtzetten, ook als de rest van dit document niet gebouwd wordt.

**De goedkeuringslaag is generiek en waardegericht.** `ApprovalSpec` (`opi/services/catalog/approval.py:94`) draagt vier callbacks: `status_of` leest de toestand, `list_items` vult de beheerpagina, `record` legt het oordeel vast, `notices_for` vertelt de aanvrager wat het betekent. De dienst `publish-on-web` declareert er twee, op waarden (`opi/services/catalog/publish_on_web/__init__.py:403`), en `send-email` declareert er een die over de dienst zelf gaat (`opi/services/catalog/send_email/__init__.py:206`, via `service_use_approval`). De generieke laag loopt de catalogus af en routeert een oordeel terug naar de eigenaar op het paar `(service, type)` (`opi/services/approvals.py:123`).

## De grens die goedkeuring niet verlegt

Onze post gaat de deur uit via de mailserver van de Rijksoverheid en draagt daarom hun domein. `rijksoverheid.nl` publiceert `p=reject`, en wij ondertekenen niet met DKIM omdat wij in die zone geen sleutel kunnen publiceren. SPF-uitlijning tussen envelope en `From:` is dus het enige dat een bericht door DMARC krijgt, en die uitlijning bestaat alleen zolang beide in `rijksoverheid.nl` zitten.

**Een eigen afzenderDOMEIN is daarmee geen goedkeuringsvraag maar een onmogelijkheid**, zolang wij op deze upstream zitten. Wat wel kan is een ander LOKAAL deel in ons eigen domein: `noreply-algoritmeregister@rijksoverheid.nl` in plaats van `noreply-rijksapp+algor-odc@rijksoverheid.nl`. Dat is wat "een eigen afzender" hier hoogstens kan betekenen, en dat is precies wat het huidige mechanisme al kan.

Daar hangt een prijs aan die niemand in code kan oplossen. Het plusdeel draagt vandaag de herleidbaarheid van een bounce: zie je `+algor-odc` terugkomen, dan weet je van wie de post was. Een eigen lokaal deel haalt die weg bij het `From:`-adres. In het voorstel hieronder houdt de ENVELOPE het afgeleide adres, dus de bounce blijft herleidbaar, maar dan lopen envelope en `From:` zichtbaar uiteen en moet dat op twee plekken zijn opgeschreven.

**Deze vraag hoort eerst bij het mailteam te liggen, niet in een sprint.** Concreet te stellen: mogen wij per project een eigen lokaal deel in `rijksoverheid.nl` voeren, wie bewaakt de naamruimte daarvan, en wat gebeurt er met post die terugkomt op een adres waar geen postbus achter zit. Dat laatste is vandaag al een gat: de upstream weigert ons eigen afzenderadres als ONTVANGER met `550 #5.1.0 Address rejected.`, en Stalwart gooit de DSN dan weg (`plans/mail-vervolgpunten.md`, punt 10). Zonder bounce-postbus vergroot een tweede adresvorm alleen het aantal adressen waar stil post verdwijnt.

## Het voorstel

Een tweede `ApprovalSpec` op de dienst `send-email`, op de projectlaag, over een waarde in plaats van over de dienst. De bestaande goedkeuring ("mag dit project mailen") blijft ongemoeid en staat los; een project kan mogen mailen zonder een eigen adres te hebben.

| | domein, bestaand | dienstgebruik, bestaand | afzenderadres, voorstel |
|---|---|---|---|
| laag | `ConfigLayer.DEPLOYMENT` | `ConfigLayer.PROJECT` | `ConfigLayer.PROJECT` |
| waarde | domeinstring, of `(domein, subdomein)` | geen, er is er een per project | de gevraagde adresstring |
| opslag | `services/[publish-on-web]/config/domains`, `status` plus `history` per item | `services/[send-email]/config/approval` | `services/[send-email]/config/from-address` plus een goedkeuringsblok ernaast |
| aanvragen | `ensure_domain_requests` | het aanzetten van de dienst is de aanvraag | het opslaan van het veld is de aanvraag, via dezelfde `post_merge`-haak (`send_email/__init__.py:193`) |
| poort | zonder goedkeuring wordt geen ingress gegenereerd | zonder goedkeuring geen secret en geen `envFrom` | zonder goedkeuring schrijft OPI de relaysleutel niet |

Vier dingen die erbij horen en die niet uit de tabel volgen:

1. **De poort zit in de schrijfweg, niet in de manifestlaag.** Bij een domein is de goedkeuring de enige waarheid, want zonder goedkeuring bestaat de ingress niet. Hier schrijft OPI een sleutel naar een relay die gelooft wat er staat. De controle hoort dus in `MailManager.ensure_account` waar `sender_address` wordt doorgegeven (`opi/manager/mail_manager.py:186`), op het pad dat werkelijk naar `set_sender` gaat, en niet alleen in `contribute_manifest_context`.

2. **De allowlist blijft het tweede slot.** `MAIL_SENDER_DOMAIN_ALLOWLIST` staat er al en moet blijven gelden voor een waarde die uit een projectbestand komt. Een goedgekeurd adres buiten ons domein is nog steeds een adres dat bij elke externe ontvanger wegvalt.

3. **Het lokale deel wordt gevalideerd zoals de weergavenaam.** Er staat al een nauwere regel dan RFC 5321 in de connector, en die bestaat omdat de waarde in een sieve-stringliteraal terechtkomt. Diezelfde regel moet gelden op het formulier en in de API, langs `ModelFieldValidator`, zodat het formulier niets toelaat wat de verwerking later laat struikelen. Dat patroon staat er voor `from-name` al (`opi/services/catalog/send_email/editables.py:28`).

4. **Intrekken moet werken.** Een goedkeuring die wordt teruggedraaid moet de sleutel op de relay WISSEN, niet alleen het veld in het projectbestand. Anders blijft een project versturen onder een adres dat het niet meer mag voeren, en dat merkt niemand. `set_sender` kent een lege waarde als "leid het af", dus de weg bestaat; de herhaalbaarheid ervan is het punt dat getest moet worden.

## Fasering

1. **De vraag bij het mailteam.** Verifieer: mag dit, onder welke voorwaarde, en wie bewaakt de naamruimte. Zonder ja hoeft de rest niet. Toets: een antwoord op schrift in `docs/ron-koppeling.md`.
2. **De docstring rechtzetten en het mechanisme afpellen.** Noteer in `opi/connectors/mail.py` dat de adresreeks vandaag nul gebruikers heeft. Toets: de tekst klopt met wat `ensure_keycloak_account` doorgeeft.
3. **Het veld plus de goedkeuring.** Editable, validatie via het model, `ApprovalSpec` erbij op de projectlaag, aanvragen via de bestaande `post_merge`-haak. Toets: een opgeslagen adres verschijnt in dezelfde beheerpagina als een domeinaanvraag, en een oordeel landt in het projectbestand met geschiedenis.
4. **De poort in de schrijfweg.** `sender_address` alleen doorgeven bij goedkeuring, intrekken wist de sleutel. Toets: twee opeenvolgende verwerkingen schrijven niets extra's, een ingetrokken goedkeuring laat het volgende bericht weer onder het afgeleide adres vertrekken, gemeten in de Mailpit-sink op de sandbox.
5. **De uitlijning aantonen.** Een bericht vanaf een project met een eigen adres, met de kopregels uit de sink in de PR: `From:` op het goedgekeurde adres, `Return-Path:` op het afgeleide adres, beide in hetzelfde domein.

## Valkuilen

**Envelope en `From:` gaan uiteenlopen.** Dat is met opzet, en het ziet eruit als een vergissing. Het staat vandaag al op twee plekken opgeschreven (de connector en de configuratie van de relay) en dat moet zo blijven, anders haalt iemand het verschil er over een jaar uit.

**De naamruimte botst met de plusdeel-conventie.** Vandaag betekent een plusdeel "dit is een project", en `MailAccountNameError` bewaakt de scheiding tussen platform- en projectnaamruimte. Een vrij lokaal deel opent een tweede naamruimte zonder eigenaar. Twee projecten die allebei `noreply-inloggen` vragen is geen technische fout maar een beheervraag, en het voorstel hierboven lost hem niet op: de goedkeurder is de enige rem. Overweeg minimaal een controle op een adres dat al aan een ander project is toegekend.

**Een sieve-script faalt stil.** `include :optional` slaat een script dat het niet kan vinden zonder een woord over, dus drift tussen de scriptnaam in OPI en die in de configuratie van de relay levert geen fout op maar post zonder naam en zonder adres. Er staat een test op die de twee aan elkaar pint; die moet meegroeien.

**Een build-error op welke sleutel dan ook bevriest de hele herbouw.** Gemeten bij RC-145: een kapot proefscript hield nieuwe waarden onzichtbaar zonder dat er iets aan die waarden mankeerde. Wie hier een tweede reeks sleutels bijzet, moet dat gedrag kennen.

## Wat er open staat

1. Mag het van het mailteam, en onder welke voorwaarde.
2. Wie bewaakt de naamruimte van lokale delen, en wat gebeurt er bij een botsing.
3. Blijft de envelope het afgeleide adres houden. Het voorstel zegt ja, om de bounce herleidbaar te houden, maar dat is een beslissing en geen gevolg.
4. Is dit de moeite waard zolang er geen bounce-postbus is. Zie `plans/mail-vervolgpunten.md`, punt 10.

## Verwante documenten

- `features/send-email.md`, de dienst zoals hij vandaag werkt, inclusief de paragraaf over waarom het adres niet te kiezen is.
- `plans/mail-vervolgpunten.md`, de openstaande punten op de mailketen, waaronder de bounces.
- `plans/de-afzender-wordt-het-project.md`, de wijziging die `from-name` een lezer gaf.
- `docs/rc159-uitrolmeting.md`, de meting waarin het afwijkende adres niet is aangetoond op productie.
