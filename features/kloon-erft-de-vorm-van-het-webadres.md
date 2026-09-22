# Een kloon erft de vorm van het webadres, niet de naam

Wat een gekloonde deployment (`cloneFrom` op `upsert_deployment`, in de praktijk de
PR-previews) overneemt van de `publish-on-web`-config van zijn bron, en wat niet.

## De regel

Het webadres van een deployment bestaat uit twee soorten instellingen.

| Helft | Instellingen | Reist mee naar de kloon |
|---|---|---|
| **Naam** | `base-domain`, `subdomain`, `issuer`, `root-component`, `expose-component-on-bare-domain` | nee |
| **Vorm** | `domain-format` | ja, tenzij hij op de naam leunt |

De naamkant mag niet mee: dan claimen twee deployments dezelfde hostnaam, vraagt een
tijdelijke preview een certificaat aan voor het productiedomein, en hangt de preview aan de
DNS-configuratie van de bron. `issuer`, `root-component` en `expose-component-on-bare-domain`
staan aan die kant omdat ze alleen iets betekenen sámen met een basisdomein dat de kloon niet
heeft: de issuer is gekozen vóór dat domein, en de andere twee worden alleen gelezen als er
een `base-domain` staat.

`domain-format` noemt geen hostnaam. Het zegt of een deployment één adres publiceert of één
per component, en twee deployments kunnen dezelfde waarde dragen zonder te botsen.

De splitsing staat in `opi/services/catalog/publish_on_web/domain_config.py`, naast
`DomainSetting`: `DOMAIN_SHAPE_SETTINGS` wordt opgesomd, `DOMAIN_NAME_SETTINGS` is de rest.
Een instelling die erbij komt valt dus vanzelf aan de naamkant, en dat is de veilige kant.

## Waarom de vorm wel mee moet

Een ontbrekende `domain-format` is geen fout. De lezers vullen hem aan met de platformdefault
van het moment waarop de deployment wordt verwerkt. Een kloon die zijn vorm kwijtraakte vroeg
dus niet om een vorm, hij erfde de default van vandaag.

Toen die default veranderde van een adres per deployment naar een adres per component
(21 juni tot 15 juli 2026), verhuisden alle PR-previews van `asses-k2n` mee zonder dat er een
regel in hun projectbestand veranderde. De SPA roept zijn API same-origin aan
(`/api/v1/...`); op een frontend-eigen hostnaam bestaat die route niet, valt het verzoek in
de nginx-catch-all voor client-side routing en komt `index.html` terug met **HTTP 200**. Geen
404, geen 502, dus het viel twee maanden niet op.

## Wanneer de vorm tóch wegvalt

Een vorm die de naam nodig heeft die net is gewist, kan niet blijven staan:

- een **puntformaat** (`component.subdomain`, `deployment.project`, ...) heeft een
  basisdomein nodig dat losse subdomeinen met punten ondersteunt. Op het clusterwildcard
  levert het een meerlaagse hostnaam op die het wildcardcertificaat niet dekt (de regel-k4c
  regressie);
- een formaat met `{subdomain}` rendert het lege subdomein als lege tekst, en levert
  `pr-857-.cluster.tld` op.

Welke formaten overblijven wordt afgeleid bij de sjablonen zelf,
`SELF_CONTAINED_FORMAT_IDS` in `opi/utils/naming.py`: vandaag `component-deployment-project`
en `deployment-project`.

## Het subdomein van de bron

Onveranderd: is het `subdomain` van de bron gelijk aan zijn deploymentnaam, dan krijgt de
kloon zijn eigen naam als subdomein. Dat is de bestaande heuristiek voor het patroon waarin
componenten één hostnaam delen en zich met paden onderscheiden.

## Wat dit niet oplost

Een deployment met een **lege** `domain-format` blijft de default van vandaag volgen, en de
kloon daarvan dus ook. Verandert die default opnieuw, dan verschuiven ze opnieuw. Het
effectieve formaat vastleggen bij het aanmaken van elke deployment is een aparte vraag, met
een migratiekant (wat doe je met de bestaande deployments die nu een lege `domain-format`
hebben?).

## Toetsen

- `tests/test_kloon_erft_de_vorm_van_het_webadres.py` — de vorm reist mee, de naam niet, en
  de kloon publiceert evenveel hostnamen als zijn bron. Die laatste is de assertie die
  `asses-k2n` had gevangen: niet het veld maar de uitkomst.
- `tests/test_publish_on_web_domain_config.py` — de splitsing dekt elke instelling precies
  één keer, en `clear_domain_name_settings` laat de vorm staan.
- `tests/test_domain_format.py` — een formaat is zelfdragend precies wanneer zijn hostnaam
  zonder naam overeind blijft.
