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

    def test_beide_regels_melden_in_een_keer(self) -> None:
        """Beide regels zitten in dezelfde haak, dus een project dat er twee breekt hoort
        ze samen te horen. Voorheen weigerde de verwijzingsraise voor de tweede regel."""
        project = _bijlageproject(
            [
                {"reference": "spook", "provide-as": "file", "path": "/etc/tls/k.p12"},
                {"reference": "keystore", "provide-as": "file", "path": "/etc/tls/k2.p12"},
                {"reference": "keystore", "provide-as": "file", "path": "/etc/tls/k3.p12"},
            ],
            ["keystore"],
        )
        with pytest.raises(ProjectIntegrityError) as fout:
            asyncio.run(validate_project_structure(project))
        assert "spook" in str(fout.value)
        assert "meervoudig gekoppeld" in str(fout.value)

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

    def test_ook_een_deployment_verderop_in_de_lijst_telt(self) -> None:
        """De regel gaat over ELKE deployment: een tweede met een langere naam maakt een
        postfix die voor de eerste past alsnog onmogelijk."""
        project = _schemaproject("rapportage", "kort")
        project["deployments"].append(_deployment("d" * 55))
        with pytest.raises(ProjectIntegrityError, match="d" * 55):
            asyncio.run(validate_project_structure(project))

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

    def test_de_schemas_van_de_zusterdienst_tellen_niet_mee(self) -> None:
        """Schema's horen bij ``postgresql-database`` (RC-17), dus hetzelfde configblok
        onder de zusterdienst is niet van deze dienst. Naast het vorige geval, dat het blok
        weghaalt en dus niet scheidt tussen "niet van mij" en "er staat toch niets"."""
        project = _schemaproject("rapportage", "d" * 55)
        project["services"] = [
            {
                "name": ServiceType.NAMESPACE_POSTGRESQL_DATABASE.value,
                "config": {"schemas": [{"postfix": "rapportage"}]},
            }
        ]
        project["components"][0]["services"] = [ServiceType.NAMESPACE_POSTGRESQL_DATABASE.value]
        assert get_service(ServiceType.POSTGRESQL_DATABASE).validate_project(project) == []


class TestDeGedeeldeLus:
    def test_twee_diensten_melden_in_dezelfde_fout(self) -> None:
        """De lus loopt de hele lijst af voor hij weigert, dus een project dat bij twee
        diensten struikelt hoort beide meldingen te krijgen en niet alleen de eerste."""
        project = _schemaproject("rapportage", "d" * 55)
        project["services"].append(
            {"attachments": {"data": [{"id": "keystore", "filename": "k.p12", "content": _VERSLEUTELD}]}}
        )
        project["components"][0]["services"].append(
            {"attachments": {"config": [{"reference": "spook", "provide-as": "file", "path": "/etc/tls/k.p12"}]}}
        )
        with pytest.raises(ProjectIntegrityError) as fout:
            asyncio.run(validate_project_structure(project))
        assert "spook" in str(fout.value)
        assert "rapportage" in str(fout.value)
