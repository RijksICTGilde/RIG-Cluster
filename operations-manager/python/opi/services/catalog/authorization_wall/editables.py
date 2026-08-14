"""Editable definitions for the authorization-wall service (project-level banner)."""

from __future__ import annotations

from opi.forms.editables.converters import EmptyToNoneConverter
from opi.forms.editables.editable import SERVICE_VIRTUALIZE, Editable
from opi.services.catalog.base import ConfigLayer, config_path
from opi.services.services_enums import ServiceType

AUTH_WALL_BANNER_EDITABLE = Editable(
    yaml_path=config_path(ConfigLayer.PROJECT, ServiceType.AUTHORIZATION_WALL, "config", "banner"),
    # Optional free-text field: an empty submission leaves no key rather than writing
    # banner: "" or null (checklist 4). banner defaults to None in the model, not a bool,
    # so remove_when_none is safe here.
    converter=EmptyToNoneConverter(),
    remove_when_none=True,
    virtualize=SERVICE_VIRTUALIZE,
)

#: Wegwijzer in het componentformulier: je zet de auth wall HIER aan, maar je stelt hem
#: ELDERS in.
#:
#: De dienst hangt aan een component (daar komt de oauth2-proxy voor de pod), terwijl zijn
#: enige instelling, de bannertekst, projectbreed is: één toegangspagina voor het hele
#: project. Wie hem hier aanvinkte zag daarna nergens wat er dan gebeurt, en de plek waar
#: dat wel staat (tabblad Services, kaart Authorization Wall) is niet te raden vanaf hier.
#:
#: Bewust alleen een verwijzing en geen tweede editor: het veld is projectbreed, dus een
#: kopie per component zou suggereren dat je het per component kunt zetten.
#:
#: Het pad wordt nooit geschreven: de visualizer staat op ``readonly``, en de verwerking
#: slaat readonly-velden over. Bewust NIET ``transient``, hoe verleidelijk dat ook klinkt
#: voor een veld zonder waarde: ``transient`` samen met ``depends_on`` betekent in deze
#: codebase al iets anders (een veld dat zijn ouder spiegelt, zoals het eigen domein), en de
#: wizard probeert zo'n veld bij het hertekenen te vullen. Dat viel om op het jokerteken in
#: het pad.
AUTH_WALL_COMPONENT_INFO_EDITABLE = Editable(
    yaml_path="components[*]/_authorization-wall-info",
    depends_on="components[*]/services",
    show_when={"contains": ServiceType.AUTHORIZATION_WALL.value},
)
