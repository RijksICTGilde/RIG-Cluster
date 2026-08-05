"""Deployments visualizer constants."""

from __future__ import annotations

from opi.forms.editables.editable import WidgetType
from opi.forms.editables.fields.deployments import (
    BACKUP_DEPLOYMENT_NAME_EDITABLE,
    BACKUP_RESOURCE_TYPES_EDITABLE,
    DEPLOYMENT_BACKUP_RESOURCE_TYPES_EDITABLE,
    DEPLOYMENT_BACKUP_SCHEDULE_DAY_EDITABLE,
    DEPLOYMENT_BACKUP_SCHEDULE_EDITABLE,
    DEPLOYMENT_BACKUP_SCHEDULE_MONTHDAY_EDITABLE,
    DEPLOYMENT_BACKUP_SCHEDULE_TIME_EDITABLE,
    DEPLOYMENT_BASE_DOMAIN_EDITABLE,
    DEPLOYMENT_CERT_COMPONENTS_SEQ_EDITABLE,
    DEPLOYMENT_CLONE_FROM_EDITABLE,
    DEPLOYMENT_CLUSTER_EDITABLE,
    DEPLOYMENT_COMP_ATTACHMENT_USE_ENV_NAME_EDITABLE,
    DEPLOYMENT_COMP_ATTACHMENT_USE_PATH_EDITABLE,
    DEPLOYMENT_COMP_ATTACHMENT_USE_PROVIDE_AS_EDITABLE,
    DEPLOYMENT_COMP_ATTACHMENT_USE_REFERENCE_EDITABLE,
    DEPLOYMENT_COMP_ATTACHMENT_USE_SEQUENCE_EDITABLE,
    DEPLOYMENT_COMP_IMAGE_EDITABLE,
    DEPLOYMENT_COMP_PUBLISH_ATTACHMENT_EDITABLE,
    DEPLOYMENT_COMP_PUBLISH_TLS_EDITABLE,
    DEPLOYMENT_COMP_PULL_POLICY_EDITABLE,
    DEPLOYMENT_COMP_REFERENCE_EDITABLE,
    DEPLOYMENT_COMPONENTS_SEQ_EDITABLE,
    DEPLOYMENT_CUSTOM_BASE_DOMAIN_EDITABLE,
    DEPLOYMENT_DOMAIN_FORMAT_EDITABLE,
    DEPLOYMENT_DOMAIN_MODE_EDITABLE,
    DEPLOYMENT_NAME_EDITABLE,
    DEPLOYMENT_REPOSITORY_EDITABLE,
    DEPLOYMENT_SUBDOMAIN_EDITABLE,
    DEPLOYMENTS_SEQUENCE_EDITABLE,
)
from opi.forms.visualizers.visualizer import EditableVisualizer
from opi.services.registry import deployment_component_service_visualizers

DEPLOYMENT_NAME = EditableVisualizer(
    editable=DEPLOYMENT_NAME_EDITABLE,
    widget=WidgetType.TEXT,
    label="Deployment naam",
    readonly_on_edit=True,
)

DEPLOYMENT_CLUSTER = EditableVisualizer(
    editable=DEPLOYMENT_CLUSTER_EDITABLE,
    widget=WidgetType.SELECT,
    label="Cluster",
)

DEPLOYMENT_REPOSITORY = EditableVisualizer(
    editable=DEPLOYMENT_REPOSITORY_EDITABLE,
    widget=WidgetType.SELECT,
    label="Repository",
)

DEPLOYMENT_SUBDOMAIN = EditableVisualizer(
    editable=DEPLOYMENT_SUBDOMAIN_EDITABLE,
    widget=WidgetType.TEXT,
    label="Subdomein",
    description="Optioneel subdomein voor deze deployment",
)

DEPLOYMENT_BASE_DOMAIN = EditableVisualizer(
    editable=DEPLOYMENT_BASE_DOMAIN_EDITABLE,
    widget=WidgetType.SELECT,
    label="Basisdomein",
    help_text="Het basisdomein voor de URL's van deze deployment",
    attributes={"data-rerender": "true"},
)

DEPLOYMENT_CUSTOM_BASE_DOMAIN = EditableVisualizer(
    editable=DEPLOYMENT_CUSTOM_BASE_DOMAIN_EDITABLE,
    widget=WidgetType.TEXT,
    label="Eigen domein",
    placeholder="voorbeeld.nl",
    help_text="Voer uw eigen domeinnaam in. U bent zelf verantwoordelijk voor DNS-configuratie. Gebruik het domein zonder 'subdomein', dus voorbeeld.nl en niet www.voorbeeld.nl",
)

DEPLOYMENT_DOMAIN_MODE = EditableVisualizer(
    editable=DEPLOYMENT_DOMAIN_MODE_EDITABLE,
    widget=WidgetType.SELECT,
    label="Domeinmodus",
    help_text=(
        "Hoe URL's worden opgebouwd: 'component-specific' geeft elk component "
        "een eigen subdomein, 'nice-url' gebruikt pad-gebaseerde routing onder één domein"
    ),
)

DEPLOYMENT_DOMAIN_FORMAT = EditableVisualizer(
    editable=DEPLOYMENT_DOMAIN_FORMAT_EDITABLE,
    widget=WidgetType.SELECT,
    label="URL-formaat",
    help_text="Het patroon waarmee hostnamen worden opgebouwd voor deze deployment.",
)

DEPLOYMENT_CLONE_FROM = EditableVisualizer(
    editable=DEPLOYMENT_CLONE_FROM_EDITABLE,
    widget=WidgetType.SELECT,
    label="Kloon data van",
    help_text="Kopieer database- en opslagdata van een andere deployment. Handig voor preview/PR-omgevingen.",
)

DEPLOYMENT_BACKUP_SCHEDULE = EditableVisualizer(
    editable=DEPLOYMENT_BACKUP_SCHEDULE_EDITABLE,
    widget=WidgetType.SELECT,
    label="Herhaling",
    help_text="Hoe vaak automatische backups worden gemaakt.",
    attributes={"data-rerender": "true"},
)
DEPLOYMENT_BACKUP_SCHEDULE_TIME = EditableVisualizer(
    editable=DEPLOYMENT_BACKUP_SCHEDULE_TIME_EDITABLE,
    widget=WidgetType.SELECT,
    label="Tijd (indicatie)",
    help_text="Rond welk tijdstip de backup wordt gestart. Dit is een indicatie, niet een exact tijdstip.",
)
DEPLOYMENT_BACKUP_SCHEDULE_DAY = EditableVisualizer(
    editable=DEPLOYMENT_BACKUP_SCHEDULE_DAY_EDITABLE,
    widget=WidgetType.SELECT,
    label="Dag van de week",
)
DEPLOYMENT_BACKUP_SCHEDULE_MONTHDAY = EditableVisualizer(
    editable=DEPLOYMENT_BACKUP_SCHEDULE_MONTHDAY_EDITABLE,
    widget=WidgetType.SELECT,
    label="Dag van de maand",
)
DEPLOYMENT_BACKUP_RESOURCE_TYPES = EditableVisualizer(
    editable=DEPLOYMENT_BACKUP_RESOURCE_TYPES_EDITABLE,
    widget=WidgetType.CHECKBOX_GROUP,
    label="Resource types",
    description="Selecteer welke resource types automatisch worden geback-upt.",
)

# Manual backup visualizers (transient)
BACKUP_DEPLOYMENT_NAME = EditableVisualizer(
    editable=BACKUP_DEPLOYMENT_NAME_EDITABLE,
    widget=WidgetType.SELECT,
    label="Deployment",
    description="Selecteer de deployment waarvoor u een backup wilt aanmaken.",
    attributes={"data-rerender": "true"},
)
BACKUP_RESOURCE_TYPES = EditableVisualizer(
    editable=BACKUP_RESOURCE_TYPES_EDITABLE,
    widget=WidgetType.CHECKBOX_GROUP,
    label="Resource types",
    description="Selecteer welke resource types u wilt back-uppen.",
)

DEPLOYMENT_COMP_REFERENCE = EditableVisualizer(
    editable=DEPLOYMENT_COMP_REFERENCE_EDITABLE,
    widget=WidgetType.SELECT,
    label="Component",
)

DEPLOYMENT_COMP_IMAGE = EditableVisualizer(
    editable=DEPLOYMENT_COMP_IMAGE_EDITABLE,
    widget=WidgetType.TEXT,
    label="Container image",
    attributes={"data-paste-clean": "container-image"},
)

DEPLOYMENT_COMP_PULL_POLICY = EditableVisualizer(
    editable=DEPLOYMENT_COMP_PULL_POLICY_EDITABLE,
    widget=WidgetType.SELECT,
    label="Pull policy",
)

DEPLOYMENT_COMP_ATTACHMENT_USE_REFERENCE = EditableVisualizer(
    editable=DEPLOYMENT_COMP_ATTACHMENT_USE_REFERENCE_EDITABLE,
    widget=WidgetType.SELECT,
    label="Bijlage",
    help_text="Kies een geuploade bijlage uit de catalogus.",
)

DEPLOYMENT_COMP_ATTACHMENT_USE_PROVIDE_AS = EditableVisualizer(
    editable=DEPLOYMENT_COMP_ATTACHMENT_USE_PROVIDE_AS_EDITABLE,
    widget=WidgetType.SELECT,
    label="Leveren als",
    help_text="Als bestand op een pad, of als waarde van een env-var (alleen tekstbestanden).",
    attributes={"data-rerender": "true"},
)

DEPLOYMENT_COMP_ATTACHMENT_USE_PATH = EditableVisualizer(
    editable=DEPLOYMENT_COMP_ATTACHMENT_USE_PATH_EDITABLE,
    widget=WidgetType.TEXT,
    label="Mount pad",
    help_text="Vereist bij 'Als bestand': het pad in de container (bijv. /etc/tls/keystore.p12).",
)

DEPLOYMENT_COMP_ATTACHMENT_USE_ENV_NAME = EditableVisualizer(
    editable=DEPLOYMENT_COMP_ATTACHMENT_USE_ENV_NAME_EDITABLE,
    widget=WidgetType.TEXT,
    label="Env-var naam",
    help_text="Vereist bij 'Als env-var': de naam van de omgevingsvariabele (bijv. CA_BUNDLE).",
)

DEPLOYMENT_COMP_ATTACHMENT_USE_SEQUENCE = EditableVisualizer(
    editable=DEPLOYMENT_COMP_ATTACHMENT_USE_SEQUENCE_EDITABLE,
    widget=WidgetType.SEQUENCE,
    label="Bijlagen",
    help_text="Koppel bijlagen specifiek voor deze deployment (overschrijft de componentkoppeling per referentie).",
    children=[
        DEPLOYMENT_COMP_ATTACHMENT_USE_REFERENCE,
        DEPLOYMENT_COMP_ATTACHMENT_USE_PROVIDE_AS,
        DEPLOYMENT_COMP_ATTACHMENT_USE_PATH,
        DEPLOYMENT_COMP_ATTACHMENT_USE_ENV_NAME,
    ],
)

DEPLOYMENT_COMPONENTS_SEQ = EditableVisualizer(
    editable=DEPLOYMENT_COMPONENTS_SEQ_EDITABLE,
    widget=WidgetType.SEQUENCE,
    label="Deployment componenten",
    children=[
        DEPLOYMENT_COMP_REFERENCE,
        DEPLOYMENT_COMP_IMAGE,
        DEPLOYMENT_COMP_PULL_POLICY,
        # Per-service deployment-component visualizers, from the registry (RC-25).
        *deployment_component_service_visualizers(),
        DEPLOYMENT_COMP_ATTACHMENT_USE_SEQUENCE,
    ],
)

DEPLOYMENT_COMP_REFERENCE_READONLY = EditableVisualizer(
    editable=DEPLOYMENT_COMP_REFERENCE_EDITABLE,
    widget=WidgetType.SELECT,
    label="Component",
    readonly_on_edit=True,
)

DEPLOYMENT_COMP_PUBLISH_TLS = EditableVisualizer(
    editable=DEPLOYMENT_COMP_PUBLISH_TLS_EDITABLE,
    widget=WidgetType.SELECT,
    label="TLS-modus",
    help_text=(
        "Erven = gebruik de instelling van het component. Anders een override voor deze "
        "deployment: standaard (platform), passthrough (cert op de pod), of aangeleverd "
        "(eigen cert op de ingress)."
    ),
    attributes={"data-rerender": "true"},
)

DEPLOYMENT_COMP_PUBLISH_ATTACHMENT = EditableVisualizer(
    editable=DEPLOYMENT_COMP_PUBLISH_ATTACHMENT_EDITABLE,
    widget=WidgetType.SELECT,
    label="Certificaat (bijlage)",
    help_text="De PEM-bijlage (cert + key) die als certificaat op de ingress komt.",
)

DEPLOYMENT_CERT_COMPONENTS_SEQ = EditableVisualizer(
    editable=DEPLOYMENT_CERT_COMPONENTS_SEQ_EDITABLE,
    widget=WidgetType.SEQUENCE,
    label="Certificaten per component",
    children=[
        DEPLOYMENT_COMP_REFERENCE_READONLY,
        DEPLOYMENT_COMP_PUBLISH_TLS,
        DEPLOYMENT_COMP_PUBLISH_ATTACHMENT,
    ],
)

DEPLOYMENTS_SEQUENCE = EditableVisualizer(
    editable=DEPLOYMENTS_SEQUENCE_EDITABLE,
    widget=WidgetType.SEQUENCE,
    label="Deployments",
    children=[
        DEPLOYMENT_NAME,
        DEPLOYMENT_CLUSTER,
        DEPLOYMENT_REPOSITORY,
        DEPLOYMENT_SUBDOMAIN,
        DEPLOYMENT_BASE_DOMAIN,
        DEPLOYMENT_CUSTOM_BASE_DOMAIN,
        DEPLOYMENT_DOMAIN_MODE,
        DEPLOYMENT_DOMAIN_FORMAT,
        DEPLOYMENT_CLONE_FROM,
        DEPLOYMENT_BACKUP_SCHEDULE,
        DEPLOYMENT_BACKUP_SCHEDULE_TIME,
        DEPLOYMENT_BACKUP_SCHEDULE_DAY,
        DEPLOYMENT_BACKUP_SCHEDULE_MONTHDAY,
        DEPLOYMENT_BACKUP_RESOURCE_TYPES,
        DEPLOYMENT_COMPONENTS_SEQ,
    ],
)
