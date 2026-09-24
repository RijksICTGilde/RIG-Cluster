# De issuer komt uit het domein, niet uit het bestand

**Status**: klaar voor de orch ship. Diagnose bewezen in productie, wijziging lokaal gebouwd en gemeten (commit `f29f686a` op `main_github`, NIET gepusht, dus de bouwstraat bouwt hem opnieuw vanaf `forgejo/main`).
**Datum**: 2026-09-23
**Aanleiding**: melding dat voor `mzs-3ik/site-admin` geen certificaat wordt aangemaakt.
**Raakt**: `operations-manager/python/opi/manager/project_manager.py`, `opi/forms/editables/generators.py`, nieuw `opi/services/catalog/publish_on_web/issuer.py`, nieuw `tests/test_publish_on_web_issuer.py`.

## Het mankement

Het veld `issuer` onder de publish-on-web-config van een deployment werd door precies een weg geschreven: de portal. `IssuerGenerator` leidt hem daar af uit het gekozen basisdomein en `apply_dependent_generators` draait bij het opslaan van het formulier. De API- en taakweg doet dat niet. `ProjectManager.configure_service` schrijft de config, laat daarna elke dienst zijn eigen ontbrekende waarden invullen (`generate_missing_values`), en alleen de uitnodigingsdienst doet daar iets. Er draait dus geen enkele formuliergenerator op die weg.

Een deployment die via zad-cli of de deploy-actie een eigen domein kreeg, kwam daarom binnen met `base-domain` en zonder `issuer`. De deploy-actie kent de invoeren `domain-format`, `subdomain` en `base-domain`, en geen `issuer`, dus de client kan het ook niet zelf meesturen.

Wat er dan gebeurt in de manifestgeneratie: `issuer_config` is leeg, dus de ingress valt terug op de cluster-issuer, en die staat voor `odcn-production` uitgecommentarieerd in `cluster_config.py` (`# "cluster_issuer": "letsencrypt-production",  # TODO: verify correct issuer name`). Beide zijn None, dus de template laat de cert-manager-annotatie weg en schrijft `tls: - {}` zonder `secretName`. Er wordt geen Issuer-resource aangemaakt en geen ACME-netwerkbeleid. Cert-manager ziet niets om te doen en de OpenShift-router serveert zijn eigen wildcard.

Niets faalt daarbij. De manifests zijn geldig, ArgoCD staat groen, de taak slaagt. Alleen de browser weet het.

## Gemeten, niet aangenomen

In `rig-prd-mzs-3ik` bestaat geen enkele Certificate en geen enkele Issuer. Ter vergelijking heeft `rig-prd-fp-unj` beide, al 77 dagen. Het adres `moza-site.rijks.app` levert een certificaat met CN `*.rig.prd1.gn2.quattro.rijksapps.nl`, dat de naam niet dekt. De gegenereerde ingress in de deployments-repo draagt geen enkele cert-manager-annotatie. De vier domeinwijzigingen voor `site-admin` op 20 september dragen allemaal het commitbericht van `configure_service`.

Over het hele projectbestandenbestand zijn er precies drie deployments met een basisdomein en zonder issuer: `mzs-3ik/site-admin`, `hwmaw-ovh/clone` en `jongo-lh2/jong-odi-prod`. Alleen de eerste draagt dat adres ook echt in zijn manifests. De tweede is het tegenvoorbeeld dat het ontwerp stuurt: dat domein is niet goedgekeurd, dus de hostnaam viel terug op het clusteradres.

## Wat er verandert

Een nieuw bestand `opi/services/catalog/publish_on_web/issuer.py` met twee functies.

`derive_issuer(project_data, deployment, cluster)` geeft de issuer die het basisdomein impliceert, ongeacht wat er is opgeslagen. Dit is letterlijk de logica die nu in `IssuerGenerator.generate` staat: per-domein-issuer uit de clusterconfig, anders voor een eigen domein de issuer uit het `domains`-blok van het project, anders `letsencrypt`.

`effective_issuer(project_data, deployment, cluster)` geeft het opgeslagen veld wanneer dat er is, en anders de afleiding. Met een poort ervoor: de afleiding gaat niet door zolang `is_deployment_domain_approved` nee zegt. Dat is de `hwmaw-ovh`-les. Valt de hostnaam terug op het clusteradres, dan mag die ingress geen issuer dragen voor een domein dat niemand aan dit project toekende, want cert-manager gaat dan een uitdaging aan voor een naam die de ingress niet bedient. Na goedkeuring verschijnt de issuer vanzelf bij de eerstvolgende verwerking, want manifests worden elke keer opnieuw uit het projectbestand gegenereerd.

Waarom een eigen bestand en niet in `domain_config.py`: de afleiding heeft `connectors/subdomain.py` nodig en dat bestand importeert `domain_config`. Andersom is een cyclus.

`IssuerGenerator.generate` roept nu `derive_issuer` aan in plaats van een eigen kopie van die logica te houden. Zo kunnen de portal en de manifestgeneratie niet uit elkaar lopen. De generator houdt bewust de poortloze variant: de portal schrijft het veld op het moment dat de gebruiker het domein kiest, dus vóór de goedkeuring, en zou hij daar wachten dan bleef het veld leeg zonder dat iets hem later opnieuw berekent.

De vier leesplekken in `project_manager.py` lezen niet langer `get_domain_setting(deployment, DomainSetting.ISSUER)` maar `effective_issuer(...)`: de helm-waardencontext, de Issuer-manifest voor helm, dezelfde voor helmfile, en de gewone deploymentweg die de component-, root- en bare-domain-ingress plus de Issuer-resource voedt.

Let op bij de helm-waardencontext: die methode had het projectbestand niet in de hand en haalt het nu op met `get_contents(record_base=False)`. Zonder dat `record_base=False` wordt die lees de compare-and-swap-basis van wie daarna opslaat, en dat brak drie tests in `test_process_project_persistence.py` en `test_process_project_scoping.py`. Die tests zijn de vangnetten hiervoor en horen in de review expliciet gedraaid te worden.

## Wat er NIET verandert

De ingress-template blijft ongemoeid. Het opgeslagen veld blijft een override, dus een project dat een eigen issuer noemt houdt die. De cluster-issuer voor `odcn-production` blijft uitgecommentarieerd; dat is een aparte vraag en geen onderdeel hiervan. Er komt geen `issuer`-invoer op de deploy-actie of de CLI, want dat zou platformkennis naar de clients verplaatsen terwijl de afleiding hier thuishoort.

## Toetsing

Nieuw `tests/test_publish_on_web_issuer.py` met de beslistabel (goedgekeurd platformdomein zonder veld geeft `letsencrypt`, opgeslagen waarde wint, geen eigen domein geeft None, niet goedgekeurd domein geeft None, niet goedgekeurd subdomein geeft None, eigen domein volgt het projectblok, en `derive_issuer` negeert de goedkeuring) plus een bronwacht in de vorm die deze repo al kent: geen enkel bestand buiten het dienstpakket mag `get_domain_setting(..., DomainSetting.ISSUER)` nog aanroepen. Dat is precies de vorm waarin dit terugkomt, namelijk een nieuwe leesplek die het ontbreken van het veld voor "geen certificaat nodig" aanziet.

Gemeten op de echte projectbestanden: over alle productieprojecten verandert precies een deployment van uitkomst, `mzs-3ik/site-admin` van None naar `letsencrypt`. `hwmaw-ovh` blijft None en `fp-unj` blijft `letsencrypt`.

Lokaal groen: ruff, ruff format, pyright, en de volledige suite op de geraakte oppervlakte (818 geslaagd over `tests/forms`, de domeintests, de manifesttests en de gouden manifests). In de volledige suite staan op deze basis twee faalpunten die van vóór deze wijziging zijn en er niets mee te maken hebben: `test_template_structure.py::test_content_blocks_are_compositions` (een contentblok van 124 regels in `bg/router.html.j2`, laatst aangeraakt op 19 augustus) en `test_attachment_schema.py::test_modal_edit_attachments_flow_carries_services_for_display`. Meet die twee op `forgejo/main` voordat je ze aan deze ship toeschrijft.

## Wat hierna nog moet, buiten deze ship

`mzs-3ik/site-admin` heeft hiermee nog geen certificaat. Er zijn twee wegen en ze sluiten elkaar niet uit. De snelle: `issuer: letsencrypt` in het publish-on-web-blok van die deployment in de projects-repo zetten en het project laten verwerken; dat werkt op de OPI die er nu draait. De structurele: deze wijziging uitrollen, waarna dezelfde verwerking het vanzelf doet en niemand het hoeft te onthouden. De DNS staat al goed, want het adres komt nu al bij de router uit, dus de ACME-uitdaging over HTTP-01 heeft een werkende weg.

Los daarvan, en een vraag voor de eigenaar en niet voor de bouwstraat: `site-admin` publiceert op `moza-site.rijks.app`, terwijl `moza-site-admin` ook is aangevraagd en goedgekeurd. Op 20 september is dat tussen 16:19 en 17:27 twee keer heen en weer gezet. Mogelijk staat de beheeromgeving nu op het adres van de site zelf.
