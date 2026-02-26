"""Domain and deployment configuration editables for the create wizard.

These fields configure the initial deployment: domain/URL strategy and
deployment identity. All fields target ``deployments[0]/...`` paths since
the wizard creates a single deployment.
"""

from __future__ import annotations

from opi.forms.editables.editable import ProjectEditable
from opi.forms.editables.validators import MinMaxLengthValidator

DOMAIN_MODE = ProjectEditable(
    yaml_path="deployments[0]/domain-mode",
    widget="select",
    label="Domeinmodus",
    default="component-specific",
    options_provider="DomainModeOptionsProvider",
    attributes={"data-rerender": "true"},
    help_text="Bepaalt hoe URL's voor uw componenten worden opgebouwd.",
)

DOMAIN_SUBDOMAIN = ProjectEditable(
    yaml_path="deployments[0]/subdomain",
    widget="text",
    label="Subdomein",
    depends_on="deployments[0]/domain-mode",
    show_when={"value": ["custom", "nice-url"]},
    help_text="Het subdomein voor uw applicatie URLs.",
)

DOMAIN_BASE_DOMAIN = ProjectEditable(
    yaml_path="deployments[0]/base-domain",
    widget="select",
    label="Basisdomein",
    options_provider="ClusterBaseDomainOptionsProvider",
    depends_on="deployments[0]/domain-mode",
    show_when={"value": "nice-url"},
    help_text="Het basisdomein voor nice-URL's. Afhankelijk van het gekozen cluster.",
)

DOMAIN_ROOT_COMPONENT = ProjectEditable(
    yaml_path="deployments[0]/root-component",
    widget="radio",
    label="Root component",
    description="Het component dat bereikbaar is op de basis-URL (bijv. mijnapp.rijks.app)",
    options_provider="ComponentReferenceOptionsProvider",
    depends_on="deployments[0]/domain-mode",
    show_when={"value": "nice-url"},
    help_text="Typisch uw frontend of single-page app. Maximaal 1 component.",
)

# ---------------------------------------------------------------------------
# Deployment identity (create wizard only)
# ---------------------------------------------------------------------------

WIZARD_DEPLOYMENT_NAME = ProjectEditable(
    yaml_path="deployments[0]/name",
    widget="text",
    label="Deployment naam",
    required=True,
    default="productie",
    validator=MinMaxLengthValidator(2, 30),
    description="Alleen kleine letters, cijfers en streepjes.",
)
