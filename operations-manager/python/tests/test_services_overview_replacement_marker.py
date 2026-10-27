"""The services overview marks a replaced service with its successor (MinIO -> SeaweedFS).

The picker and the save-time validation already keep a replaced service out of new
projects (RC-382). The overview page is read-only, so what it owes the reader is the
marker that says why. These tests pin the marker: without them, an overview that forgets
the pill reads exactly like "recommended service" to anyone browsing the catalog.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from opi.core.config import settings
from opi.core.templates_lotc import templates_lotc
from opi.services.services_enums import ServiceType
from opi.web.services_router import replacement_marker

if TYPE_CHECKING:
    import pytest


class TestDeMarkeringVolgtDeCluster:
    def test_vervangen_waar_de_opvolger_bestaat(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "CLUSTER_MANAGER", "sandboxed-local")
        assert replacement_marker(ServiceType.MINIO_STORAGE) == "SeaweedFS Object Storage"

    def test_de_opvolger_zelf_draagt_geen_markering(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "CLUSTER_MANAGER", "sandboxed-local")
        assert replacement_marker(ServiceType.SEAWEEDFS_STORAGE) is None

    def test_niets_op_een_cluster_zonder_opvolger(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "CLUSTER_MANAGER", "local")
        assert replacement_marker(ServiceType.MINIO_STORAGE) is None


def _kaart_service(replaced_by: str | None) -> dict[str, Any]:
    """De minimale kaartdie de macro nodig heeft; geen variabelen, geen help."""
    return {
        "name": "minio-storage",
        "label": "MinIO Object Storage",
        "icon": "wolk",
        "color": "default",
        "help_template": None,
        "summary": "Objectopslag",
        "variables": [],
        "replaced_by": replaced_by,
    }


class _TemplatingLike:
    """Haal de Jinja-omgeving uit Jinja2Templates, ongeacht de wrapper-versie."""

    @staticmethod
    def env() -> Any:
        return getattr(templates_lotc, "env", templates_lotc)


class TestDeLotcKaart:
    def test_met_markering(self) -> None:
        macro = _TemplatingLike.env().get_template("bg/_service-card.html.j2").module.service_card
        html = str(macro(_kaart_service("SeaweedFS Object Storage")))
        assert "Vervangen door SeaweedFS Object Storage" in html

    def test_zonder_markering(self) -> None:
        macro = _TemplatingLike.env().get_template("bg/_service-card.html.j2").module.service_card
        html = str(macro(_kaart_service(None)))
        assert "Vervangen door" not in html


class TestDeOudeVariant:
    def _render(self, replaced_by: str | None) -> str:
        definition = SimpleNamespace(
            name="MinIO Object Storage",
            description="Objectopslag",
            icon="wolk",
            color="default",
            help_template=None,
        )
        service_info = SimpleNamespace(
            definition=definition,
            service=ServiceType.MINIO_STORAGE,
            variables=[],
            replaced_by=replaced_by,
        )
        template = _TemplatingLike.env().get_template("services-overview/_diensten.html.j2")
        return template.render(services=[service_info])

    def test_met_markering(self) -> None:
        assert "Vervangen door SeaweedFS Object Storage" in self._render("SeaweedFS Object Storage")

    def test_zonder_markering(self) -> None:
        assert "Vervangen door" not in self._render(None)
