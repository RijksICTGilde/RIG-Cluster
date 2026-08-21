# Service guide: toepassingsgerichte uitleg naast de korte help

## Wat het is

Elke service heeft een korte uitleg (`help.md`: wat is dit, wanneer gebruik je het) die de portal als popup toont en de API als `explanation` teruggeeft. Sommige services hebben daarnaast een verhaal dat daar niet in past zonder de lezer dood te gooien: de scenario's waar een gebruiker in terechtkomt en welke velden elk scenario configureren. Dat is de guide: een tweede markdown per service, optioneel, in hetzelfde kleine dialect als `help.md` (titel, secties, alinea's, bullets, bold, links; zie `opi/services/help_text.py`).

De eerste service met een guide is publish-on-web: het domeinenverhaal (domain-format-varianten, subdomeinen, eigen domein, paden per component, wanneer wel of niet expose-component-on-bare-domain, de aanvraagprocedure).

## Hoe je het gebruikt

- **API**: `GET /api/v2/services/{name}` heeft een veld `guide` met de markdown; `null` voor een service zonder guide.
- **Portal**: dezelfde helproute die de popup bedient rendert de guide als volledige pagina, bijvoorbeeld `/forms/wizard/help/publish_on_web/guide.md`. De korte help van publish-on-web linkt ernaar onder "Meer scenario's".
- **CLI**: de markdown zit in het `guide`-veld van het describe-antwoord; hoe zadctl hem toont is aan de CLI (bijvoorbeeld een `service guide`-subcommando).

## Een guide toevoegen aan een service

1. Schrijf `guide.md` in het servicepakket, naast `help.md`, in het kleine markdown-dialect. Toepassingsgericht: per scenario wat je instelt en wat je dan krijgt.
2. Zet `guide_template="<pakket>/guide.md"` op de `ServiceDefinition`.
3. Klaar: de API serveert hem als `guide`, de portal rendert hem via de helproute met het icoon van de service. Meer is er niet; `tests/test_service_help_markdown.py` bewaakt dat elke gedeclareerde guide bestaat, met een titel begint en zonder restjes componentmarkup rendert.

De goedkeuringsbanner (approval_specs) staat bewust alleen boven de popup-help, niet boven de guide: een guide benoemt per scenario zelf wat een aanvraag is en wat niet.

## Afhankelijkheden

- `opi/services/help_text.py`: `service_guide_markdown()`, en de bestaande render- en resolutiemechaniek.
- `opi/web/router_wizard.py`: de bestaande helproute; er is geen nieuwe route.
- `GET /api/v2/services/{name}` (`opi/api/v2/router.py`): het `guide`-veld.
