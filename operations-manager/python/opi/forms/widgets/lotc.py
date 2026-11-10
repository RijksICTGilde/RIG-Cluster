"""Form widgets, rendered with Lord of the Components.

The widgets in ``templates_lotc/widgets/`` use ``:prop="expr"`` and ``:attrs="<dict>"``, so
they pass the extension's preprocessor without objection. They are therefore rendered in the
LOTC environment itself: ONE step, and an error in a component call surfaces right away
instead of while picking a string apart.
"""

import markupsafe

from opi.forms.widgets.fields import FieldWidgetAdapter
from opi.http.render.templates_lotc import templates_lotc


class LOTCWidgetAdapter(FieldWidgetAdapter):
    """Renders form fields with Lord of the Components.

    Inherits the preparation per field type (which options, which value, how a sequence is
    built): that is domain logic and does not change along with the component system. What
    this adds is WHERE the template comes from and in which environment it renders.
    """

    def __init__(self) -> None:
        self._env = templates_lotc.env

    #: The context keys carrying ALREADY RENDERED HTML instead of text: the children of a
    #: row, a column, a fieldset, a button bar or a sequence item. The renderer builds them
    #: itself out of the widgets below.
    _HTML_SUFFIXES = ("_html", "_content")

    def _render_template(self, template_name: str, ctx: dict[str, object]) -> str:
        return self._env.get_template(f"widgets/{template_name}").render(**self._mark_html(ctx))

    @classmethod
    def _mark_html(cls, ctx: dict[str, object]) -> dict[str, object]:
        """Mark the already rendered HTML in ``ctx`` as safe.

        Needed because this environment has autoescape ON, which the component system
        demands. Without it a template inserting its children's HTML with
        ``{{ child_html }}`` yields the LITERAL text ``<nldd-button ...>`` on the screen
        rather than a card with fields, and only for a sequence WITH items: an empty form has
        no children to lose.

        Here and not with ``| safe`` in every template, because it is a property of the
        ENVIRONMENT and not of a template: a new widget template would otherwise make the
        same mistake again.

        Safe because these values were built by the renderer itself out of the widgets below,
        where the DATA in them was already escaped, and because nothing here is put through a
        template engine a second time.
        """
        marked: dict[str, object] = {}
        for key, value in ctx.items():
            if not key.endswith(cls._HTML_SUFFIXES):
                marked[key] = value
            elif isinstance(value, str):
                marked[key] = markupsafe.Markup(value)  # noqa: S704  # HTML already rendered by the component layer, not input
            elif isinstance(value, list):
                marked[key] = [
                    markupsafe.Markup(part) if isinstance(part, str) else part  # noqa: S704  # HTML already rendered by the component layer, not input
                    for part in value  # pyright: ignore[reportUnknownVariableType]
                ]
            else:
                marked[key] = value
        return marked

    def render_flow(self, children_html: list[str]) -> str:
        """The outer stack around the fields, rendered right away.

        The source is a CONSTANT, so the preprocessor and the compiler only ever see the tag
        and never the already rendered fields. Those go in as a variable and are therefore not
        run as a template a second time, which is the whole point: that HTML carries values
        somebody typed into the form.

        ``c-stack`` and NOT ``c-layout-flow``: the latter becomes an ``<nldd-container>``
        under NLDD, which puts no space between its children, and the explanation under a
        field then looks like it belongs to the NEXT label. ``c-stack`` is the primitive that
        does set a gap.
        """
        template = self._env.from_string('<c-stack gap="lg">{{ inner }}</c-stack>')
        return template.render(inner=markupsafe.Markup("\n".join(children_html)))  # noqa: S704  # already rendered field HTML, goes into the template as a variable
