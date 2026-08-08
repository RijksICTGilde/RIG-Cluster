"""Voorbeeldvelden om de omgezette formulierlaag te kunnen bekijken.

De formulierlaag is de zwaarste stap van de omzetting geweest en tegelijk de enige die
je niet op een gewone pagina ziet: hij zit in de wizard, en die heeft een echt project
nodig. Zonder iets als dit zou de laag wel omgezet zijn maar niet te beoordelen.

Wat hier staat zijn ECHTE ``FormField``-objecten, geen nagemaakt project. Dat verschil
is belangrijk: een verzonnen projectbestand zou er in een screenshot uitzien als een
bestaand project, en dat is precies het soort beeld dat later voor waar wordt
aangezien. Een veld met het label "Projectnaam" is onmiskenbaar een voorbeeld.

De reeks dekt elk veldtype dat de applicatie kent, plus de standen die in de omzetting
konden sneuvelen: een veld met hulptekst, een met een foutmelding, een uitgeschakeld
veld, en een met htmx-attributen.
"""

from opi.forms.field import FormField

EXAMPLE_FIELDS: list[FormField] = [
    FormField(
        name="display_name",
        path="display_name",
        schema_type=str,
        widget_type="text",
        label="Projectnaam",
        required=True,
        placeholder="bijvoorbeeld mijn-project",
        help_text="Alleen kleine letters, cijfers en streepjes.",
        value="algoritmeregister",
    ),
    FormField(
        name="description",
        path="description",
        schema_type=str,
        widget_type="textarea",
        label="Omschrijving",
        placeholder="Waar is dit project voor?",
        attributes={"rows": "3"},
    ),
    # Een veld met een foutmelding: lotc-forms leidt de foutstaat hieruit af, dus dit
    # toetst meteen dat de aparte invalid-vlag terecht is weggevallen.
    FormField(
        name="email",
        path="users[0].email",
        schema_type=str,
        widget_type="text",
        label="E-mailadres beheerder",
        required=True,
        value="geen-adres",
        errors=["Dit is geen geldig e-mailadres."],
    ),
    # Een veld met htmx: de attribuutbundel moet op het invoerveld landen.
    FormField(
        name="subdomain",
        path="deployments[0].subdomain",
        schema_type=str,
        widget_type="text",
        label="Subdomein",
        help_text="Wordt gecontroleerd terwijl u typt.",
        htmx_attrs={"hx-get": "/forms/check-subdomain", "hx-trigger": "keyup changed delay:500ms"},
    ),
    FormField(
        name="memory",
        path="components[0].resources.memory",
        schema_type=int,
        widget_type="number",
        label="Geheugenlimiet (MiB)",
        value=512,
        attributes={"min": "128", "max": "8192", "step": "64"},
    ),
    FormField(
        name="cluster",
        path="clusters[0]",
        schema_type=str,
        widget_type="select",
        label="Cluster",
        required=True,
        value="odcn-production",
        options=[
            {"value": "odcn-production", "label": "ODC-Noord productie"},
            {"value": "sandboxed-local", "label": "Sandbox (lokaal)"},
        ],
    ),
    FormField(
        name="expires",
        path="expires_at",
        schema_type=str,
        widget_type="date",
        label="Verloopt op",
        help_text="Laat leeg voor onbeperkt.",
    ),
    FormField(
        name="publish",
        path="services.publish-on-web",
        schema_type=bool,
        widget_type="checkbox",
        label="Publiceren op het web",
        value=True,
    ),
    # Een uitgeschakeld veld: readonly hoorde in de roos-versie bij bool_attr en is nu
    # :disabled. Zichtbaar moeten blijven dat het er staat maar niet aanpasbaar is.
    FormField(
        name="api_key",
        path="config.api-key",
        schema_type=str,
        widget_type="text",
        label="API-sleutel",
        value="wordt automatisch gegenereerd",
        readonly=True,
        help_text="Dit veld wordt door het platform beheerd.",
    ),
]
