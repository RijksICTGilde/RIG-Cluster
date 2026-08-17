"""Non-destructive edit tests using real project YAML files.

Every modal edit flow that targets a list item (deployments, components)
MUST preserve all existing project data. These tests load a real project
file, simulate what _modal_do_submit does, and verify that only the
intended field changed while everything else is preserved.

This catches the class of bug where a flow does not declare its target,
causing the save to replace the entire project.

De dienstLAAG stond hier niet in, en dat is precies waarom dezelfde klasse fout daar
opnieuw kon ontstaan: een lijst in dienstconfiguratie die het formulier niet toonde
werd bij elke opslag leeggeschreven. ``TestDienstconfiguratieLijsten`` onderaan dekt
die laag datagedreven over de catalogus, met per lijstveld een geval, zodat elke dienst
die er later bij komt gratis meeloopt.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, ClassVar

import pytest
import yaml
from opi.forms.editables.editable import WidgetType, apply_virtualize
from opi.forms.editables.path import resolve_path
from opi.forms.editables.processor import EditableFormProcessor
from opi.forms.editables.rendered_sequences import GERENDERDE_REEKSEN_VELD
from opi.forms.editables.service_path import smart_delete_value, smart_get_value, smart_set_value
from opi.forms.visualizers.fields.components import COMPONENTS_SEQUENCE
from opi.forms.visualizers.flows import get_flow
from opi.forms.visualizers.sections import FormSection
from opi.forms.wizard.save import apply_list_item_merge as _apply_list_item_merge
from opi.forms.wizard.state import CLEARED_FIELD
from opi.services.catalog.base import ConfigLayer
from opi.services.registry import SERVICES

FIXTURES_DIR = Path(__file__).parent / "e2e" / "fixtures" / "projects"
UNIT_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "projects"
PROJECTS_DIR = Path(__file__).parent.parent.parent.parent / "projects"


def _load_project(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _collect_project_files() -> list[Path]:
    """Collect all available project YAML fixtures."""
    files = []
    for d in [FIXTURES_DIR, UNIT_FIXTURES_DIR, PROJECTS_DIR]:
        if d.exists():
            files.extend(d.glob("*.yaml"))
    return files


def _simulate_modal_save(
    existing_data: dict,
    merged_data: dict,
    flow_id: str,
) -> dict:
    """Simulate the merge logic from _modal_do_submit.

    Returns the resulting project data after the simulated save.
    This replicates the exact logic: detect list target, apply list
    item merge or fall through to update.
    """
    result = copy.deepcopy(existing_data)
    merged = copy.deepcopy(merged_data)

    target = get_flow(flow_id).target
    if target is not None:
        _apply_list_item_merge(result, merged, target.list_key, target.index, target.is_new)
        merged.pop(target.list_key, None)

    result.update(merged)
    return result


# ---------------------------------------------------------------------------
# Parametrized non-destructive tests for every deployment-targeting flow
# ---------------------------------------------------------------------------

DEPLOYMENT_FLOW_CASES = [
    (
        "modal-edit-deployment-0",
        {"deployments": [{"domain-format": "component-deployment-project"}]},
        "domain-format",
    ),
    (
        "modal-edit-domain-0",
        {"deployments": [{"domain-format": "project"}]},
        "domain-format",
    ),
    (
        "modal-edit-backup-schedule-0",
        {"deployments": [{"backup": {"schedule": "daily"}}]},
        "backup",
    ),
]


class TestNonDestructiveDeploymentEdits:
    """Verify that editing a single deployment field preserves all other data."""

    @pytest.fixture
    def project_data(self) -> dict:
        """A realistic project with multiple top-level keys and deployments."""
        return {
            "name": "test-project",
            "display-name": "Test Project",
            "description": "A test project",
            "clusters": ["local"],
            "services": ["publish-on-web", "keycloak"],
            "users": [{"email": "admin@test.nl", "role": "admin"}],
            "config": {"age-public-key": "age1abc123"},
            "components": [
                {"name": "web", "publish-on-web": True, "type": "deployment"},
                {"name": "worker", "type": "deployment"},
            ],
            "repositories": [
                {"name": "main", "url": "ssh://git@host/repo.git", "branch": "main"},
            ],
            "deployments": [
                {
                    "name": "productie",
                    "cluster": "local",
                    "namespace": "test-project",
                    "repository": "main",
                    "domain-format": "component-deployment-project",
                    "components": [
                        {"reference": "web", "image": "nginx:latest"},
                        {"reference": "worker", "image": "python:3.13"},
                    ],
                },
                {
                    "name": "staging",
                    "cluster": "local",
                    "namespace": "test-project",
                    "repository": "main",
                    "components": [
                        {"reference": "web", "image": "nginx:1.25"},
                    ],
                },
            ],
        }

    @pytest.mark.parametrize(("flow_id", "merged_data", "changed_key"), DEPLOYMENT_FLOW_CASES)
    def test_edit_preserves_all_other_fields(
        self,
        project_data: dict,
        flow_id: str,
        merged_data: dict,
        changed_key: str,
    ) -> None:
        """The edit must only modify the targeted field; everything else stays."""
        original = copy.deepcopy(project_data)
        result = _simulate_modal_save(project_data, merged_data, flow_id)

        # Top-level keys outside deployments must be untouched
        for key in original:
            if key == "deployments":
                continue
            assert result[key] == original[key], f"Top-level key '{key}' was modified"

        # All deployments must still exist
        assert len(result["deployments"]) == len(original["deployments"])

        # The targeted deployment (index 0) must keep all its other fields
        original_dep = original["deployments"][0]
        result_dep = result["deployments"][0]
        for key in original_dep:
            if key == changed_key:
                continue
            assert result_dep[key] == original_dep[key], f"Deployment field '{key}' was modified by {flow_id}"

        # The non-targeted deployment must be completely untouched
        assert result["deployments"][1] == original["deployments"][1]

    def test_backup_schedule_on_second_deployment(self, project_data: dict) -> None:
        """Setting backup schedule on deployment[1] must not touch deployment[0]."""
        original = copy.deepcopy(project_data)
        merged = {"deployments": [{}, {"backup": {"schedule": "weekly"}}]}
        result = _simulate_modal_save(project_data, merged, "modal-edit-backup-schedule-1")

        # First deployment untouched
        assert result["deployments"][0] == original["deployments"][0]

        # Second deployment gets backup, keeps everything else
        assert result["deployments"][1]["backup"] == {"schedule": "weekly"}
        assert result["deployments"][1]["name"] == "staging"
        assert result["deployments"][1]["cluster"] == "local"
        assert result["deployments"][1]["components"] == original["deployments"][1]["components"]


class TestNonDestructiveComponentEdits:
    """Verify that editing a component preserves all other data."""

    @pytest.fixture
    def project_data(self) -> dict:
        return {
            "name": "test-project",
            "components": [
                {"name": "web", "publish-on-web": True, "type": "deployment", "ports": {"inbound": [8080]}},
                {"name": "worker", "type": "deployment"},
            ],
            "deployments": [
                {"name": "prod", "cluster": "local", "components": [{"reference": "web", "image": "nginx:latest"}]},
            ],
        }

    def test_edit_component_preserves_project(self, project_data: dict) -> None:
        original = copy.deepcopy(project_data)
        merged = {"components": [{"ports": {"inbound": [9090]}}]}
        result = _simulate_modal_save(project_data, merged, "modal-edit-component-0")

        # Component 0 updated, but keeps other fields
        assert result["components"][0]["ports"] == {"inbound": [9090]}
        assert result["components"][0]["name"] == "web"
        assert result["components"][0]["publish-on-web"] is True

        # Component 1 untouched
        assert result["components"][1] == original["components"][1]

        # Deployments untouched
        assert result["deployments"] == original["deployments"]


class TestClearedFieldEdits:
    """Clearing a field in a component/deployment edit must delete it.

    A plain dict.update cannot express a deleted key, so the wizard carries
    a CLEARED_FIELD tombstone for fields the user emptied (built with
    get_merged_data(strip_cleared=False)). _apply_list_item_merge must drop
    the tombstoned key instead of resurrecting the old value.
    """

    @pytest.fixture
    def project_data(self) -> dict:
        return {
            "name": "test-project",
            "components": [
                {
                    "name": "web",
                    "type": "deployment",
                    "ports": {"inbound": [8080]},
                    "aliases": {"REDIS_HOSTS": "redis://web.redis:6379"},
                },
                {"name": "worker", "type": "deployment"},
            ],
            "deployments": [
                {"name": "prod", "cluster": "local", "components": [{"reference": "web", "image": "nginx:latest"}]},
            ],
        }

    def test_clearing_aliases_removes_the_key(self, project_data: dict) -> None:
        original = copy.deepcopy(project_data)
        # Tombstone on aliases = user emptied the aliases field.
        merged = {"components": [{"aliases": CLEARED_FIELD, "ports": {"inbound": [8080]}}]}
        result = _simulate_modal_save(project_data, merged, "modal-edit-component-0")

        assert "aliases" not in result["components"][0], "Cleared aliases was resurrected"
        # Untouched fields survive, tombstone never reaches the saved data.
        assert result["components"][0]["name"] == "web"
        assert result["components"][0]["ports"] == {"inbound": [8080]}
        assert CLEARED_FIELD not in result["components"][0].values()
        # Sibling component untouched.
        assert result["components"][1] == original["components"][1]

    def test_clearing_deployment_field_removes_the_key(self, project_data: dict) -> None:
        project_data["deployments"][0]["domain-format"] = "project"
        merged = {"deployments": [{"domain-format": CLEARED_FIELD}]}
        result = _simulate_modal_save(project_data, merged, "modal-edit-deployment-0")

        assert "domain-format" not in result["deployments"][0], "Cleared deployment field was resurrected"
        assert result["deployments"][0]["name"] == "prod"


class TestUnregisteredFlowDetection:
    """Verify that every list-targeting flow family declares its target."""

    EXPECTED_DEPLOYMENT_PREFIXES: ClassVar[list[str]] = [
        "modal-edit-deployment-",
        "modal-add-deployment-",
        "modal-edit-domain-",
        "modal-edit-backup-schedule-",
    ]

    EXPECTED_COMPONENT_PREFIXES: ClassVar[list[str]] = [
        "modal-edit-component-",
    ]

    @pytest.mark.parametrize("prefix", EXPECTED_DEPLOYMENT_PREFIXES)
    def test_deployment_prefix_declares_its_target(self, prefix: str) -> None:
        target = get_flow(f"{prefix}0").target
        assert target is not None, f"Flow '{prefix}0' declares no target"
        assert target.list_key == "deployments"

    @pytest.mark.parametrize("prefix", EXPECTED_COMPONENT_PREFIXES)
    def test_component_prefix_declares_its_target(self, prefix: str) -> None:
        target = get_flow(f"{prefix}0").target
        assert target is not None, f"Flow '{prefix}0' declares no target"
        assert target.list_key == "components"


class TestAgainstRealProjectFiles:
    """Run non-destructive checks against every real project YAML fixture.

    This ensures that simulating a backup schedule save on any real
    project file does not lose data.
    """

    @staticmethod
    def _project_files() -> list[Path]:
        return _collect_project_files()

    @pytest.fixture(params=_collect_project_files(), ids=lambda p: p.name)
    def real_project(self, request: pytest.FixtureRequest) -> dict:
        return _load_project(request.param)

    def test_backup_schedule_preserves_all_data(self, real_project: dict) -> None:
        """Setting a backup schedule on deployment 0 must not lose any data."""
        if not real_project.get("deployments"):
            pytest.skip("No deployments in project file")

        original = copy.deepcopy(real_project)
        merged = {"deployments": [{"backup": {"schedule": "daily"}}]}
        result = _simulate_modal_save(real_project, merged, "modal-edit-backup-schedule-0")

        # Every top-level key must still exist
        for key in original:
            assert key in result, f"Top-level key '{key}' was deleted"

        # Every top-level key except deployments must be identical
        for key in original:
            if key == "deployments":
                continue
            assert result[key] == original[key], f"Top-level key '{key}' was modified"

        # Same number of deployments
        assert len(result["deployments"]) == len(original["deployments"])

        # Non-targeted deployments are identical
        for i in range(1, len(original["deployments"])):
            assert result["deployments"][i] == original["deployments"][i], f"Deployment {i} was modified"

        # Targeted deployment keeps all original fields
        orig_dep = original["deployments"][0]
        result_dep = result["deployments"][0]
        for key in orig_dep:
            if key == "backup":
                continue
            assert key in result_dep, f"Deployment field '{key}' was deleted"
            assert result_dep[key] == orig_dep[key], f"Deployment field '{key}' was modified"

        # The backup schedule was set by the merge
        assert result_dep.get("backup") == {"schedule": "daily"}

    def test_domain_edit_preserves_all_data(self, real_project: dict) -> None:
        """Changing domain-format on deployment 0 must not lose any data."""
        if not real_project.get("deployments"):
            pytest.skip("No deployments in project file")

        original = copy.deepcopy(real_project)
        merged = {"deployments": [{"domain-format": "project"}]}
        result = _simulate_modal_save(real_project, merged, "modal-edit-domain-0")

        # Every top-level key except deployments must be identical
        for key in original:
            if key == "deployments":
                continue
            assert result[key] == original[key], f"Top-level key '{key}' was modified"

        # Same number of deployments
        assert len(result["deployments"]) == len(original["deployments"])

        # Targeted deployment keeps all original fields
        orig_dep = original["deployments"][0]
        result_dep = result["deployments"][0]
        for key in orig_dep:
            if key == "domain-format":
                continue
            assert result_dep[key] == orig_dep[key], f"Deployment field '{key}' was modified"


# ---------------------------------------------------------------------------
# De dienstlaag: elke lijst in dienstconfiguratie, datagedreven over de catalogus
# ---------------------------------------------------------------------------

#: Herkenbare inhoud die na de verwerking nog moet staan.
_BEWAARDE_ITEMS = [{"__toets__": "eerste"}, {"__toets__": "tweede"}]


def _dienstsectie_reeksen() -> list[tuple[str, FormSection, Any]]:
    """Reeksen die een dienstsectie op haar EIGEN niveau verwerkt.

    Een sectie van de componentlaag draagt paden met ``[*]`` erin; die wordt niet als
    sectie gerenderd maar als kinderen van de componentenreeks, waar de index pas
    ontstaat. Die lopen hieronder mee via ``_geneste_reeksen``.
    """
    gevonden = []
    for service_type, service in SERVICES.items():
        for layer in ConfigLayer:
            section = service.config_form_section(layer)
            if section is None:
                continue
            gevonden.extend(
                (f"{service_type.value}-{layer.value}-{vis.editable.yaml_path}", section, vis)
                for vis in section.editables
                if vis.widget == WidgetType.SEQUENCE and "[*]" not in vis.editable.yaml_path
            )
    return gevonden


def _geneste_reeksen() -> list[tuple[str, FormSection, Any]]:
    """Reeksen die binnen de componentenreeks zitten.

    Zonder de paden met een ``{dienst}``-filter erin. Die lijsten hangen onder de
    dienstSELECTIE van het component, en de selectie bepaalt of de reeks getekend
    wordt: staat de dienst aan, dan staat de lijst op het scherm. "Wel gekozen, niet
    getekend" is geen toestand die het formulier kan maken, en een toets die hem
    nabouwt meet de overlay van de componentenrij en niet de regel hierboven.
    ``test_montages_overleven_een_stroom_die_ze_niet_kent`` dekt wat daar wel kan
    misgaan.
    """
    sectie = FormSection(section_id="componenten-toets", title="Componenten", editables=[COMPONENTS_SEQUENCE])
    return [
        (vis.editable.yaml_path, sectie, vis)
        for vis in COMPONENTS_SEQUENCE.children or []
        if vis.widget == WidgetType.SEQUENCE and "{" not in vis.editable.yaml_path
    ]


def _alle_reeksen() -> list[tuple[str, FormSection, Any]]:
    return [*_dienstsectie_reeksen(), *_geneste_reeksen()]


def _seed(vis: Any) -> tuple[dict[str, Any], str]:
    """Projectgegevens met deze lijst gevuld, plus het concrete pad ernaartoe.

    ``[*]`` wordt op index 0 gezet, en een ``depends_on`` met ``contains`` wordt zo
    gevuld dat de reeks daadwerkelijk zichtbaar is -- anders slaat de verwerker hem
    over en meet de toets niets.
    """
    ed = vis.editable
    pad = resolve_path(ed.yaml_path, 0)
    data: dict[str, Any] = {}

    # Maak de lijsten aan waar het pad doorheen loopt (components[0], deployments[0]).
    segmenten = pad.split("/")
    for i, segment in enumerate(segmenten):
        if "[" not in segment:
            continue
        sleutel = segment.split("[")[0]
        ouder = "/".join(segmenten[:i])
        houder = smart_get_value(data, ouder) if ouder else data
        if isinstance(houder, dict):
            houder.setdefault(sleutel, [{}])

    if ed.depends_on and isinstance(ed.show_when, dict) and "contains" in ed.show_when:
        smart_set_value(data, resolve_path(ed.depends_on, 0), [ed.show_when["contains"]])

    smart_set_value(data, pad, copy.deepcopy(_BEWAARDE_ITEMS))
    return data, pad


async def _verwerk(section: FormSection, data: dict[str, Any], pad: str, getekend: list[str]) -> dict[str, Any]:
    """Verwerk een inzending die alles draagt BEHALVE de lijst onder toets.

    Precies de toestand die het echte formulier oplevert: de omliggende rijen komen mee
    (anders wordt de reeks eromheen niet eens verwerkt en meet de toets niets), en over
    deze ene lijst staat er niets in. Wat dat betekent, zegt ``getekend``.
    """
    inzending = copy.deepcopy(data)
    smart_delete_value(inzending, pad)
    inzending[GERENDERDE_REEKSEN_VELD] = getekend
    resultaat, _errors = await EditableFormProcessor().process_json_submission(
        inzending,
        section.editables,
        data,
        edit_mode=True,
    )
    return resultaat


class TestDienstconfiguratieLijsten:
    """Een lijst in dienstconfiguratie verdwijnt niet, en kan wel leeggemaakt worden.

    Deze klasse bestaat omdat de dienstlaag hier ontbrak: dit bestand beschermde
    deployments en componenten en kende geen enkele toets op dienstconfiguratie, en
    daar liep een lijst leeg zonder dat iets klaagde.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("naam", "section", "vis"), _alle_reeksen(), ids=[naam for naam, _, _ in _alle_reeksen()])
    async def test_niet_getoonde_lijst_blijft_staan(self, naam: str, section: FormSection, vis: Any) -> None:
        """Een inzending die deze lijst niet tekende mag hem niet vervangen."""
        data, pad = _seed(vis)
        resultaat = await _verwerk(section, data, pad, getekend=["een/andere/reeks"])

        assert smart_get_value(resultaat, pad) == _BEWAARDE_ITEMS, (
            f"{naam} verloor {pad} terwijl het formulier de lijst niet toonde"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("naam", "section", "vis"), _alle_reeksen(), ids=[naam for naam, _, _ in _alle_reeksen()])
    async def test_getoonde_lijst_kan_leeg(self, naam: str, section: FormSection, vis: Any) -> None:
        """De andere kant: wie de laatste regel weghaalt houdt geen lijst over.

        Zonder deze helft ruilt de vorige toets gegevensverlies in voor een lijst die
        je niet meer leeg kunt maken.
        """
        data, pad = _seed(vis)
        virt = vis.editable.virtualize
        getekend = [pad, apply_virtualize(pad, virt)] if virt else [pad]
        resultaat = await _verwerk(section, data, pad, getekend=getekend)

        overgebleven = smart_get_value(resultaat, pad)
        assert overgebleven in ([], None), f"{naam} hield {pad} op {overgebleven!r} terwijl de gebruiker hem leegde"
