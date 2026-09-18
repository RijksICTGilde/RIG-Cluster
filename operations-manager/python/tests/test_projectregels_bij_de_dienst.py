"""De projectbrede regels hangen aan de dienst die ze bezit (RC-182).

Elke regel wordt op de POORT gemeten en niet alleen op zijn functie: de haak is pas iets
waard als de lus in ``validate_project_structure`` hem ook echt aanroept. De functies
zelf houden hun eigen toetsen (``test_attachment_schema.py``,
``test_database_schemas_api.py``).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from opi.core.project_schema import ProjectIntegrityError
from opi.manager.project_validation import validate_project_structure
from opi.services.registry import get_service, project_validating_services
from opi.services.services_enums import ServiceType

_POSTGRES = ServiceType.POSTGRESQL_DATABASE.value
_VERSLEUTELD = "-----BEGIN AGE ENCRYPTED FILE-----\ndata\n-----END AGE ENCRYPTED FILE-----"


def _deployment(name: str = "deployment-1", components: list[dict] | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "cluster": "local",
        "namespace": "demo",
        "components": components if components is not None else [{"reference": "api"}],
    }


def _bijlageproject(koppelingen: list[dict[str, Any]], catalogus: list[str]) -> dict[str, Any]:
    return {
        "schema-version": 2,
        "name": "demo",
        "services": [
            {
                "attachments": {
                    "data": [{"id": naam, "filename": f"{naam}.p12", "content": _VERSLEUTELD} for naam in catalogus]
                }
            }
        ],
        "components": [{"name": "api", "type": "single", "services": [{"attachments": {"config": koppelingen}}]}],
        "deployments": [_deployment()],
    }


def _schemaproject(postfix: str, deploymentnaam: str) -> dict[str, Any]:
    return {
        "schema-version": 2,
        "name": "demo",
        "services": [{"name": _POSTGRES, "config": {"scope": "shared", "schemas": [{"postfix": postfix}]}}],
        "components": [{"name": "api", "type": "single", "services": [_POSTGRES]}],
        "deployments": [_deployment(deploymentnaam)],
    }


class TestBijlagenOpDePoort:
    def test_onbekende_verwijzing_wordt_geweigerd(self) -> None:
        project = _bijlageproject(
            [{"reference": "spook", "provide-as": "file", "path": "/etc/tls/k.p12"}], ["keystore"]
        )
        with pytest.raises(ProjectIntegrityError, match="spook"):
            asyncio.run(validate_project_structure(project))

    def test_dubbele_koppeling_wordt_geweigerd(self) -> None:
        """Alleen ``validate_attachment_couplings`` ziet dit: het schema dekt de
        dienstenlijst van een basiscomponent niet."""
        project = _bijlageproject(
            [
                {"reference": "keystore", "provide-as": "file", "path": "/etc/tls/k.p12"},
                {"reference": "keystore", "provide-as": "file", "path": "/etc/tls/tweede.p12"},
            ],
            ["keystore"],
        )
        with pytest.raises(ProjectIntegrityError, match="meervoudig gekoppeld"):
            asyncio.run(validate_project_structure(project))

    def test_een_kloppend_project_komt_erdoor(self) -> None:
        asyncio.run(
            validate_project_structure(
                _bijlageproject(
                    [{"reference": "keystore", "provide-as": "file", "path": "/etc/tls/k.p12"}], ["keystore"]
                )
            )
        )

    def test_ook_een_project_zonder_de_dienst(self) -> None:
        """Een certificaat van publish-on-web noemt ook een bijlage, dus de dienst
        weglaten mag de regel niet omzeilen."""
        project = {
            "schema-version": 2,
            "name": "demo",
            "services": [{"publish-on-web": {"config": {"tls": "provided", "attachment": "spook"}}}],
            "components": [{"name": "api", "type": "single", "services": ["publish-on-web"]}],
            "deployments": [_deployment()],
        }
        with pytest.raises(ProjectIntegrityError, match="spook"):
            asyncio.run(validate_project_structure(project))

    def test_de_regels_hangen_aan_de_dienst(self) -> None:
        dienst = get_service(ServiceType.ATTACHMENTS)
        assert dienst in project_validating_services()
        fouten = dienst.validate_project(
            _bijlageproject([{"reference": "spook", "provide-as": "file", "path": "/x"}], ["keystore"])
        )
        assert len(fouten) == 1
        assert "spook" in fouten[0]


class TestSchemanamenOpDePoort:
    def test_te_lange_samengestelde_naam_wordt_geweigerd(self) -> None:
        with pytest.raises(ProjectIntegrityError, match="rapportage"):
            asyncio.run(validate_project_structure(_schemaproject("rapportage", "d" * 55)))

    def test_een_naam_die_past_komt_erdoor(self) -> None:
        asyncio.run(validate_project_structure(_schemaproject("rapportage", "deployment-1")))

    def test_de_regel_hangt_aan_de_dienst(self) -> None:
        dienst = get_service(ServiceType.POSTGRESQL_DATABASE)
        assert dienst in project_validating_services()
        fouten = dienst.validate_project(_schemaproject("rapportage", "d" * 55))
        assert len(fouten) == 1
        assert "rapportage" in fouten[0]

    def test_zonder_de_dienst_zegt_de_regel_niets(self) -> None:
        project = _schemaproject("rapportage", "d" * 55)
        project["services"] = ["publish-on-web"]
        project["components"][0]["services"] = ["publish-on-web"]
        assert get_service(ServiceType.POSTGRESQL_DATABASE).validate_project(project) == []
