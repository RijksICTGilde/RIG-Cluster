"""Een portaalbewerking mag het helmfile-blok niet weggooien.

Twee van de projecten op het platform (`mb-docs-helmfile`, `mb-grist-helmfile`)
worden niet uit componenten opgebouwd maar uit een helmfile. Die vorm is met de
hand geschreven: geen enkel formulier in ``opi/forms/`` noemt ``helmfile``, en
geen wizardstap kan hem bewerken. De vraag die daaronder ligt is niet of je hem
kunt maken, maar of je hem KWIJTRAAKT wanneer iemand zo'n project via het
portaal aanpast -- een teamlid toevoegen, een domein wijzigen.

Het antwoord is nee, en deze test is waarom: sinds RC-26 schrijft een
modalbewerking alleen de paden die haar eigen editables noemen
(``opi/forms/wizard/write_set.py``), dus een blok dat geen enkele editable kent
wordt niet aangeraakt. Dat is een eigenschap van de schrijfverzameling, niet van
helmfile -- en precies daarom hoort er een test op te staan voordat er iets aan
de wizard verandert dat die eigenschap opgeeft.

De harnas is die van ``test_flow_write_isolation``: dezelfde echte opslagweg,
hier op een project dat er een helmfile-catalogus en een helmfile-verwijzing bij
heeft. Vergelijking is byte-voor-byte op de gedumpte YAML.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from opi.forms.editables.service_path import smart_get_value, smart_set_value
from opi.utils.yaml_util import dump_yaml_to_string
from tests.forms.test_flow_write_isolation import (
    FLOW_EDITS,
    account_for_resource_intent,
    build_project,
    run_flow_edit,
)

# De helm-values op deploymentniveau staan in het echte projectbestand als AGE-blok;
# die op projectniveau als gewone boom. Beide vormen komen hier voor, want een
# bewerking die er niet over gaat moet ze allebei ongemoeid laten.
HELM_VALUES_VERSLEUTELD = "-----BEGIN AGE ENCRYPTED FILE-----\naGVsbXZhbHVlcw==\n-----END AGE ENCRYPTED FILE-----\n"

GOTMPL = (
    'bases:\n  - "helmfile/bases/environment.yaml.gotmpl"\n\n---\n\n'
    'bases:\n  - "helmfile/bases/default.yaml.gotmpl"\n\n'
    'helmfiles:\n  - path: "helmfile/apps/docs/helmfile-child.yaml.gotmpl"\n'
    "    values:\n      - {{ toYaml .Values | nindent 8 }}\n"
)


def helmfile_catalogus() -> list[dict[str, Any]]:
    """Het catalogusitem op projectniveau, met alles wat de schemavorm toelaat."""
    return [
        {
            "name": "mb-docs",
            "url": "https://github.com/example/mijn-bureau-infra.git",
            "ref": "main",
            "path": "helmfile",
            "files": {"helmfile.yaml.gotmpl": GOTMPL},
            "helm-values": {
                "application": {"docs": {"enabled": True}, "grist": {"enabled": False}},
                "autoscaling": {"horizontal": {"docs": {"backend": {"enabled": False}}}},
            },
            "services": ["publish-on-web", "keycloak", "namespace-postgresql-database"],
        }
    ]


def helmfile_verwijzing() -> list[dict[str, Any]]:
    """Het verwijzingsitem op deploymentniveau."""
    return [
        {
            "reference": "mb-docs",
            "env-vars": {"DJANGO_SETTINGS_MODULE": "impress.settings"},
            "helm-values": HELM_VALUES_VERSLEUTELD,
        }
    ]


def build_helmfile_project() -> dict[str, Any]:
    """Het project uit de schrijfisolatietest, met een helmfile erbij."""
    data = build_project()
    data["helmfile"] = helmfile_catalogus()
    data["deployments"][0]["helmfile"] = helmfile_verwijzing()
    return data


@pytest.mark.parametrize(
    ("flow_id", "yaml_path", "new_value", "flow_context"),
    FLOW_EDITS,
    ids=[f"{f}:{p}" for f, p, _v, _c in FLOW_EDITS],
)
@pytest.mark.asyncio
async def test_bewerking_laat_het_helmfile_blok_heel(
    flow_id: str,
    yaml_path: str,
    new_value: Any,
    flow_context: dict[str, Any],
) -> None:
    """Elke bewerkstroom wijzigt zijn eigen veld en verder niets aan de helmfile."""
    project_data = build_helmfile_project()

    expected = copy.deepcopy(project_data)
    smart_set_value(expected, yaml_path, new_value)

    result = await run_flow_edit(project_data, flow_id, yaml_path, new_value, **flow_context)

    assert smart_get_value(result, yaml_path) == new_value, f"{flow_id} paste zijn eigen wijziging niet toe"
    account_for_resource_intent(result, expected, flow_id, yaml_path, new_value)
    assert dump_yaml_to_string(result) == dump_yaml_to_string(expected), (
        f"{flow_id} wijzigde meer dan {yaml_path} aan een project met een helmfile"
    )


@pytest.mark.parametrize(
    ("flow_id", "yaml_path", "new_value", "flow_context"),
    FLOW_EDITS,
    ids=[f"{f}:{p}" for f, p, _v, _c in FLOW_EDITS],
)
@pytest.mark.asyncio
async def test_de_helmfile_velden_komen_ongeschonden_terug(
    flow_id: str,
    yaml_path: str,
    new_value: Any,
    flow_context: dict[str, Any],
) -> None:
    """Genoemd in plaats van gedumpt: de blokken zelf, veld voor veld.

    De byte-vergelijking hierboven vangt dit ook, maar zegt bij rood alleen "er
    is meer veranderd". Deze zegt WAT er weg is -- de catalogus, de verwijzing,
    het versleutelde blok, de gotmpl-bestanden, of de dienstenlijst binnen het
    item.
    """
    result = await run_flow_edit(build_helmfile_project(), flow_id, yaml_path, new_value, **flow_context)

    assert result.get("helmfile") == helmfile_catalogus(), f"{flow_id} raakte de helmfile-catalogus"

    verwijzing = result["deployments"][0].get("helmfile")
    assert verwijzing == helmfile_verwijzing(), f"{flow_id} raakte de helmfile-verwijzing van de deployment"
    assert verwijzing[0]["helm-values"] == HELM_VALUES_VERSLEUTELD, (
        f"{flow_id} veranderde de versleutelde helm-values van de deployment"
    )
