"""
Tests for opi.api.endpoint_util module.

Tests parse_ports, normalize_project_name and validate_api_token.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from opi.api.endpoint_util import (
    REQUIRES_METRICS_SCRAPE_TOKEN,
    REQUIRES_PROJECT_NAME,
    image_push_refusal,
    normalize_project_name,
    parse_ports,
    validate_api_token,
    validate_metrics_scrape_token,
)
from opi.api.image_router import push_image
from opi.core.actor import get_actor, project_api_key_actor
from opi.core.authz.actions import Action, action
from opi.core.authz.engine import Cause
from opi.core.leden import LidSoort
from opi.services.project_leden import hash_van
from starlette.requests import Request

_VER_IN_DE_TOEKOMST = datetime.now(UTC) + timedelta(days=30)


class TestParsePorts:
    """Tests for parse_ports pure function."""

    def test_single_port(self):
        assert parse_ports("8080") == [8080]

    def test_multiple_ports(self):
        assert parse_ports("80,443,8080") == [80, 443, 8080]

    def test_empty_string(self):
        assert parse_ports("") == []

    def test_whitespace_handling(self):
        assert parse_ports(" 80 , 443 , 8080 ") == [80, 443, 8080]

    def test_invalid_port_returns_empty(self):
        assert parse_ports("abc") == []

    def test_mixed_valid_invalid_returns_empty(self):
        """Whole string fails if any port is not a number."""
        assert parse_ports("80,abc,443") == []

    def test_trailing_comma(self):
        assert parse_ports("80,443,") == [80, 443]


class TestNormalizeProjectName:
    """Tests for normalize_project_name pure function."""

    def test_lowercase_conversion(self):
        assert normalize_project_name("MyProject") == "myproject"

    def test_removes_special_chars(self):
        assert normalize_project_name("my-project!@#") == "myproject"

    def test_keeps_underscores(self):
        assert normalize_project_name("my_project") == "my_project"

    def test_keeps_numbers(self):
        assert normalize_project_name("project123") == "project123"

    def test_empty_string(self):
        assert normalize_project_name("") == ""

    def test_all_special_chars(self):
        assert normalize_project_name("!@#$%^&*()") == ""

    def test_spaces_removed(self):
        assert normalize_project_name("my project") == "myproject"


class TestValidateApiToken:
    """Tests for validate_api_token decorator."""

    @pytest.mark.asyncio
    async def test_missing_api_key_header(self):
        """Should return 401 when X-API-Key header is missing."""

        @action(Action.PROJECT_READ)
        @validate_api_token
        async def dummy_route(request: Request, project_name: str):
            return {"ok": True}

        mock_request = MagicMock(spec=Request)
        mock_request.headers = {}

        with pytest.raises(HTTPException) as exc_info:
            await dummy_route(request=mock_request, project_name="test")

        assert exc_info.value.status_code == 401
        assert "Authentication required" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_missing_project_name(self):
        """Should return 401 when project_name is not provided."""

        @action(Action.PROJECT_READ)
        @validate_api_token
        async def dummy_route(request: Request, project_name: str = ""):
            return {"ok": True}

        mock_request = MagicMock(spec=Request)
        mock_request.headers = {"X-API-Key": "some-key"}

        with pytest.raises(HTTPException) as exc_info:
            await dummy_route(request=mock_request, project_name="")

        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_invalid_api_key(self):
        """Should return 401 when API key does not match project."""

        @action(Action.PROJECT_READ)
        @validate_api_token
        async def dummy_route(request: Request, project_name: str = ""):
            return {"ok": True}

        mock_request = MagicMock(spec=Request)
        mock_request.scope = {}
        mock_request.headers = {"X-API-Key": "wrong-key"}

        mock_project = MagicMock()
        mock_project.api_key = "correct-key"
        mock_project.name = "test-project"

        mock_service = MagicMock()
        mock_service.get.return_value = mock_project

        with (
            patch("opi.api.endpoint_util.get_project_store", return_value=mock_service),
            pytest.raises(HTTPException) as exc_info,
        ):
            await dummy_route(request=mock_request, project_name="test-project")

        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_non_ascii_api_key(self):
        """Een niet-ASCII teken in de header is een foute sleutel, geen TypeError."""

        @action(Action.PROJECT_READ)
        @validate_api_token
        async def dummy_route(request: Request, project_name: str = ""):
            return {"ok": True}

        mock_request = MagicMock(spec=Request)
        mock_request.scope = {}
        mock_request.headers = {"X-API-Key": "caf\xe9"}

        mock_project = MagicMock()
        mock_project.api_key = "correct-key"
        mock_project.name = "test-project"

        mock_service = MagicMock()
        mock_service.get.return_value = mock_project

        with (
            patch("opi.api.endpoint_util.get_project_store", return_value=mock_service),
            pytest.raises(HTTPException) as exc_info,
        ):
            await dummy_route(request=mock_request, project_name="test-project")

        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_valid_api_key(self):
        """Should call through when API key matches project."""

        @action(Action.PROJECT_READ)
        @validate_api_token
        async def dummy_route(request: Request, project_name: str = ""):
            return {"project": project_name}

        mock_request = MagicMock(spec=Request)
        mock_request.headers = {"X-API-Key": "correct-key"}

        mock_project = MagicMock()
        mock_project.api_key = "correct-key"
        mock_project.name = "test-project"

        mock_service = MagicMock()
        mock_service.get.return_value = mock_project

        with patch("opi.api.endpoint_util.get_project_store", return_value=mock_service):
            result = await dummy_route(request=mock_request, project_name="test-project")

        assert result == {"project": "test-project"}

    @pytest.mark.asyncio
    async def test_valid_api_key_sets_the_project_actor(self):
        """De actor van een projectsleutel legitimeert een project, geen persoon."""

        @action(Action.PROJECT_READ)
        @validate_api_token
        async def dummy_route(request: Request, project_name: str = ""):
            return {"actor": get_actor()}

        mock_request = MagicMock(spec=Request)
        mock_request.headers = {"X-API-Key": "correct-key"}

        mock_project = MagicMock()
        mock_project.api_key = "correct-key"
        mock_project.name = "test-project"

        mock_service = MagicMock()
        mock_service.get.return_value = mock_project

        with patch("opi.api.endpoint_util.get_project_store", return_value=mock_service):
            result = await dummy_route(request=mock_request, project_name="test-project")

        assert result == {"actor": project_api_key_actor("test-project")}

    @pytest.mark.asyncio
    async def test_project_not_found(self):
        """Should return 401 when project is not found."""

        @action(Action.PROJECT_READ)
        @validate_api_token
        async def dummy_route(request: Request, project_name: str = ""):
            return {"ok": True}

        mock_request = MagicMock(spec=Request)
        mock_request.scope = {}
        mock_request.headers = {"X-API-Key": "some-key"}

        mock_service = MagicMock()
        mock_service.get.return_value = None

        with (
            patch("opi.api.endpoint_util.get_project_store", return_value=mock_service),
            pytest.raises(HTTPException) as exc_info,
        ):
            await dummy_route(request=mock_request, project_name="nonexistent")

        assert exc_info.value.status_code == 401


class TestImagePushRefusal:
    """``image_push_refusal`` is de voorcontrole van de image-push: vóór de
    multipart-parser weten of deze aanroeper ``image.push`` MAG, en niet alleen of hij bij
    het project hoort. Een tarball van gigabytes hoort de tempschijf niet te raken voor
    een token dat de push toch niet opent."""

    def _project(self, naam: str, token_waarde: str, rollen: list[str]) -> SimpleNamespace:
        return SimpleNamespace(
            name=naam,
            api_key=None,
            data={},
            users=[
                SimpleNamespace(
                    type=LidSoort.SERVICE_ACCOUNT,
                    name="bouwstraat",
                    principaal="bouwstraat",
                    email=None,
                    roles=rollen,
                    tokens=[SimpleNamespace(name="pijplijn", expires=_VER_IN_DE_TOEKOMST, hash=hash_van(token_waarde))],
                )
            ],
        )

    def _request(self) -> Request:
        return Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/v1/projects/eigen-project/images/push",
                "path_params": {},
                "query_string": b"image_name=app&tag=v1",
                "headers": [],
                "client": ("10.0.0.1", 1234),
            }
        )

    async def _weigering(self, presented: str | None, project_name: str, project: Any) -> HTTPException | None:
        store = MagicMock()
        store.get.return_value = project
        with patch("opi.api.endpoint_util.get_project_store", return_value=store):
            return await image_push_refusal(self._request(), presented, project_name, push_image)

    @pytest.mark.asyncio
    async def test_een_token_met_de_rol_komt_erdoor(self) -> None:
        project = self._project("eigen-project", "zad_token", ["developer"])
        assert await self._weigering("zad_token", "eigen-project", project) is None

    @pytest.mark.asyncio
    async def test_een_te_smalle_rol_wordt_403_voor_de_parser(self) -> None:
        """Een viewer hoort bij het project en is dus herkend, maar opent ``image.push``
        niet. Dat is de weigering die vóór deze taak pas na 5 GB tempschijf viel."""
        project = self._project("eigen-project", "zad_token", ["viewer"])

        weigering = await self._weigering("zad_token", "eigen-project", project)

        assert weigering is not None
        assert weigering.status_code == 403
        assert weigering.detail == {"cause": Cause.INSUFFICIENT_ROLE, "message": weigering.detail["message"]}

    @pytest.mark.asyncio
    async def test_een_onbekend_token_geeft_401(self) -> None:
        """Een token dat dit project niet draagt (bijvoorbeeld van een ander project) is
        hier niet te onderscheiden van een geraden token: beide geven ``NotRecognised``,
        want de zoektocht gebeurt binnen het project uit de URL en dat project draagt
        deze hash niet."""
        project = self._project("eigen-project", "zad_vaneigen", ["ops"])

        weigering = await self._weigering("zad_vanandermans", "eigen-project", project)

        assert weigering is not None
        assert weigering.status_code == 401
        assert weigering.detail == "Invalid API key"

    @pytest.mark.asyncio
    async def test_zonder_bewijsmiddel_geeft_401(self) -> None:
        project = self._project("eigen-project", "zad_token", ["ops"])

        weigering = await self._weigering(None, "eigen-project", project)

        assert weigering is not None
        assert weigering.status_code == 401
        assert weigering.detail == "Authentication required - provide X-API-Key header"

    @pytest.mark.asyncio
    async def test_een_onbekend_project_geeft_401(self) -> None:
        weigering = await self._weigering("zad_token", "onbestaand-project", None)

        assert weigering is not None
        assert weigering.status_code == 401
        assert weigering.detail == "Invalid API key"


class TestMarkeringen:
    """Elke herkenningsdecorator laat op de route zien dat hij er zit.

    Zonder die markering valt een route niet te onderscheiden van een route zonder enige
    controle, en dat is precies wat ``tests/test_elke_route_heeft_een_herkenning.py`` moet
    kunnen zien.
    """

    @pytest.mark.parametrize(
        ("decorator", "markering"),
        [
            (validate_api_token, REQUIRES_PROJECT_NAME),
            (validate_metrics_scrape_token, REQUIRES_METRICS_SCRAPE_TOKEN),
        ],
    )
    def test_de_decorator_zet_zijn_eigen_markering(self, decorator, markering):
        async def dummy_route(request: Request):
            return {}

        assert getattr(decorator(dummy_route), markering, False) is True

    def test_elke_markering_is_een_eigen_naam(self):
        """Twee decorators met dezelfde markering zijn in een route niet te onderscheiden."""
        markeringen = [
            REQUIRES_PROJECT_NAME,
            REQUIRES_METRICS_SCRAPE_TOKEN,
        ]

        assert len(set(markeringen)) == len(markeringen)

    def test_een_decorator_zet_niet_de_markering_van_een_ander(self):
        """Anders zou een route twee herkenningen lijken te eisen."""

        async def dummy_route(request: Request):
            return {}

        gedecoreerd = validate_api_token(dummy_route)

        assert getattr(gedecoreerd, REQUIRES_METRICS_SCRAPE_TOKEN, False) is False
