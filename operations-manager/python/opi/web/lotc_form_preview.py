"""Example fields, so the form layer can be looked at without a real project.

These are REAL ``FormField`` objects and not a made-up project file: a fabricated project
looks like an existing one in a screenshot, and that is the kind of image a reader later
believes. Keep the labels unmistakably examples.

The series covers every widget type the application knows, plus the states that are easy to
lose: a field with help text, one with an error, a read-only one, and one with htmx attributes.
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
    # lotc-forms derives the error state from ``errors``; there is no separate invalid flag.
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
    # With htmx: the attribute bundle has to land on the input itself.
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
    # Read-only: visible, and not editable.
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
