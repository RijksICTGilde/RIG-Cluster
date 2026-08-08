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

    def _render_template(self, template_name: str, ctx: dict[str, object]) -> str:
        return self._env.get_template(f"widgets/{template_name}").render(**ctx)
