"""Deployments visualizer constants."""

from __future__ import annotations

from opi.forms.editables.editable import WidgetType
from opi.forms.editables.fields.deployments import (
    DEPLOYMENT_BASE_DOMAIN_EDITABLE,
    DEPLOYMENT_CLONE_FROM_EDITABLE,
    DEPLOYMENT_CLUSTER_EDITABLE,
    DEPLOYMENT_COMP_IMAGE_EDITABLE,
    DEPLOYMENT_COMP_PULL_POLICY_EDITABLE,
    DEPLOYMENT_COMP_REFERENCE_EDITABLE,
    DEPLOYMENT_COMPONENTS_SEQ_EDITABLE,
    DEPLOYMENT_DOMAIN_MODE_EDITABLE,
    DEPLOYMENT_NAME_EDITABLE,
    DEPLOYMENT_REPOSITORY_EDITABLE,
    DEPLOYMENT_SUBDOMAIN_EDITABLE,
    DEPLOYMENTS_SEQUENCE_EDITABLE,
)
from opi.forms.visualizers.visualizer import EditableVisualizer

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

DEPLOYMENT_CLONE_FROM = EditableVisualizer(
    editable=DEPLOYMENT_CLONE_FROM_EDITABLE,
    widget=WidgetType.SELECT,
    label="Kloon data van",
    help_text="Kopieer database- en opslagdata van een andere deployment. Handig voor preview/PR-omgevingen.",
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
)

DEPLOYMENT_COMP_PULL_POLICY = EditableVisualizer(
    editable=DEPLOYMENT_COMP_PULL_POLICY_EDITABLE,
    widget=WidgetType.SELECT,
    label="Pull policy",
)

DEPLOYMENT_COMPONENTS_SEQ = EditableVisualizer(
    editable=DEPLOYMENT_COMPONENTS_SEQ_EDITABLE,
    widget=WidgetType.SEQUENCE,
    label="Deployment componenten",
    children=[DEPLOYMENT_COMP_REFERENCE, DEPLOYMENT_COMP_IMAGE, DEPLOYMENT_COMP_PULL_POLICY],
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
        DEPLOYMENT_DOMAIN_MODE,
        DEPLOYMENT_CLONE_FROM,
        DEPLOYMENT_COMPONENTS_SEQ,
    ],
)
