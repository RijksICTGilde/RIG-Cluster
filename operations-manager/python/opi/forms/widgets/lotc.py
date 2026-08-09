"""Formulierwidgets voor de LOTC-bouwlijn.

De tegenhanger van ``roos.py``, met een belangrijk verschil in de werkwijze.

De ROOS-adapter rendert een widget in een KALE Jinja-omgeving en levert een string met
``<c-*>``-tags op; die tags worden pas later omgezet, door het ``process_components``-
filter. Dat moest wel, want de roos-widgets schrijven Jinja-expressies op attribuut-
positie en de voorbewerker zou daarop breken.

Bij LOTC is dat niet nodig en ook niet wenselijk. De omgezette widgets in
``templates_lotc/widgets/`` gebruiken ``:prop="expr"`` en ``:attrs="<dict>"``, dus ze
komen zonder bezwaar door de voorbewerker. Ze worden hier daarom in de LOTC-omgeving
zelf gerenderd: één stap, en fouten in een componentaanroep komen meteen naar boven in
plaats van pas bij het napluizen van een string.
"""

import markupsafe

from opi.core.templates_lotc import templates_lotc
from opi.forms.widgets.roos import ROOSWidgetAdapter


class LOTCWidgetAdapter(ROOSWidgetAdapter):
    """Rendert formuliervelden met Lord of the Components in plaats van jinja-roos.

    Erft van de ROOS-adapter zodat alle voorbereiding per veldtype (welke opties, welke
    waarde, hoe een reeks wordt opgebouwd) gedeeld blijft: dat is bedrijfslogica en
    verandert niet mee met het componentensysteem. Alleen WAAR de template vandaan komt
    en in welke omgeving hij rendert, verandert.
    """

    def __init__(self) -> None:
        # Bewust niet super().__init__(): die zet een kale omgeving op templates/ .
        self._env = templates_lotc.env

    #: De contextsleutels die AL GERENDERDE HTML dragen in plaats van tekst: de kinderen
    #: van een rij, een kolom, een fieldset, een knoppenbalk of een reeks-item. De
    #: renderer bouwt ze zelf op uit de widgets hieronder.
    _HTML_SUFFIXEN = ("_html", "_content")

    def _render_template(self, template_name: str, ctx: dict[str, object]) -> str:
        return self._env.get_template(f"widgets/{template_name}").render(**self._markeer_html(ctx))

    @classmethod
    def _markeer_html(cls, ctx: dict[str, object]) -> dict[str, object]:
        """Merk de al gerenderde HTML in ``ctx`` aan als veilig.

        HIER LOOPT HET VERSCHIL MET DE ROOS-ADAPTER, en het is er maar een: die rendert
        in een KALE omgeving met autoescape UIT, deze in de LOTC-omgeving met autoescape
        AAN (dat is een eis van het componentensysteem). Een sjabloon dat de HTML van zijn
        kinderen met ``{{ child_html }}`` invoegt levert daardoor hier geen kaart met
        velden op maar de LETTERLIJKE tekst ``<nldd-button ...>`` op het scherm.

        Zichtbaar werd dat pas bij een reeks MET items - een leeg formulier heeft geen
        kinderen om te verliezen - en dus in de bewerkdialoog van een bestaand project
        (Projectleden beheren, Component bewerken) eerder dan in de aanmaakwizard.

        Waarom hier en niet met ``| safe`` in elk sjabloon: het is een eigenschap van de
        OMGEVING, niet van een sjabloon. Een nieuw widgetsjabloon zou de vergissing anders
        opnieuw maken, en de sjablonen zijn gedeeld met de roos-bouwlijn.

        Veilig omdat deze waarden door de renderer zelf zijn opgebouwd uit de widgets
        hieronder; de GEGEVENS erin zijn daar al geescaped. Wat hier NIET gebeurt, en dat
        is het verschil dat telt: er wordt niets opnieuw door een sjabloonmotor gehaald.
        """
        gemarkeerd: dict[str, object] = {}
        for sleutel, waarde in ctx.items():
            if not sleutel.endswith(cls._HTML_SUFFIXEN):
                gemarkeerd[sleutel] = waarde
            elif isinstance(waarde, str):
                gemarkeerd[sleutel] = markupsafe.Markup(waarde)  # noqa: S704
            elif isinstance(waarde, list):
                gemarkeerd[sleutel] = [
                    markupsafe.Markup(deel) if isinstance(deel, str) else deel  # noqa: S704
                    for deel in waarde  # pyright: ignore[reportUnknownVariableType]
                ]
            else:
                gemarkeerd[sleutel] = waarde
        return gemarkeerd

    def render_flow(self, children_html: list[str]) -> str:
        """De buitenste stapel om de velden, meteen gerenderd.

        De bron is een CONSTANTE - de voorbewerker en de compiler zien alleen de tag,
        nooit de al gerenderde velden. Die komen er als variabele in, en worden dus geen
        tweede keer als sjabloon uitgevoerd. Dat verschil is het hele punt: die HTML
        draagt waarden die iemand in het formulier heeft getypt.
        """
        # c-stack en NIET c-layout-flow. Die laatste wordt onder NLDD een
        # <nldd-container>, en die zet geen tussenruimte tussen zijn kinderen: de velden
        # kwamen tegen elkaar aan te staan, waardoor de uitleg onder een veld bij het
        # VOLGENDE label leek te horen. Op een screenshot meteen zichtbaar, in de HTML
        # niet - de velden zijn er allemaal en elk attribuut klopt.
        # c-stack is de primitief die wel een gap zet (.lotc-stack, display:flex).
        template = self._env.from_string('<c-stack gap="lg">{{ inner }}</c-stack>')
        return template.render(inner=markupsafe.Markup("\n".join(children_html)))  # noqa: S704
