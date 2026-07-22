"""
Tests for opi.api.endpoint_util module.

Tests parse_ports, normalize_project_name, validate_api_token, and validate_master_api_key.
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from opi.api.endpoint_util import (
    normalize_project_name,
    parse_ports,
    validate_api_token,
    validate_master_api_key,
)
from starlette.requests import Request


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

        @validate_api_token
        async def dummy_route(request: Request, project_name: str = ""):
            return {"ok": True}

        mock_request = MagicMock(spec=Request)
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
    async def test_valid_api_key(self):
        """Should call through when API key matches project."""

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
    async def test_project_not_found(self):
        """Should return 401 when project is not found."""

        @validate_api_token
        async def dummy_route(request: Request, project_name: str = ""):
            return {"ok": True}

        mock_request = MagicMock(spec=Request)
        mock_request.headers = {"X-API-Key": "some-key"}

        mock_service = MagicMock()
        mock_service.get.return_value = None

        with (
            patch("opi.api.endpoint_util.get_project_store", return_value=mock_service),
            pytest.raises(HTTPException) as exc_info,
        ):
            await dummy_route(request=mock_request, project_name="nonexistent")

        assert exc_info.value.status_code == 401


class TestValidateMasterApiKey:
    """Tests for validate_master_api_key decorator."""

    @pytest.mark.asyncio
    async def test_missing_api_key_header(self):
        """Should return 401 when X-API-Key header is missing."""

        @validate_master_api_key
        async def dummy_route(request: Request):
            return {"ok": True}

        mock_request = MagicMock(spec=Request)
        mock_request.headers = {}

        with pytest.raises(HTTPException) as exc_info:
            await dummy_route(request=mock_request)

        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_master_key_not_configured(self):
        """Should return 501 when MASTER_API_KEY is not set."""

        @validate_master_api_key
        async def dummy_route(request: Request):
            return {"ok": True}

        mock_request = MagicMock(spec=Request)
        mock_request.headers = {"X-API-Key": "some-key"}

        with patch("opi.api.endpoint_util.settings") as mock_settings:
            mock_settings.MASTER_API_KEY = None
            with pytest.raises(HTTPException) as exc_info:
                await dummy_route(request=mock_request)

        assert exc_info.value.status_code == 501

    @pytest.mark.asyncio
    async def test_invalid_master_key(self):
        """Should return 401 when master key does not match."""

        @validate_master_api_key
        async def dummy_route(request: Request):
            return {"ok": True}

        mock_request = MagicMock(spec=Request)
        mock_request.headers = {"X-API-Key": "wrong-key"}

        with patch("opi.api.endpoint_util.settings") as mock_settings:
            mock_settings.MASTER_API_KEY = "correct-master-key"
            with pytest.raises(HTTPException) as exc_info:
                await dummy_route(request=mock_request)

        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_valid_master_key(self):
        """Should call through when master key matches."""

        @validate_master_api_key
        async def dummy_route(request: Request):
            return {"admin": True}

        mock_request = MagicMock(spec=Request)
        mock_request.headers = {"X-API-Key": "correct-master-key"}

        with patch("opi.api.endpoint_util.settings") as mock_settings:
            mock_settings.MASTER_API_KEY = "correct-master-key"
            result = await dummy_route(request=mock_request)

        assert result == {"admin": True}
