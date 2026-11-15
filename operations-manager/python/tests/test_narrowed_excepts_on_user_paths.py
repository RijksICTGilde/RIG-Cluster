"""The narrowed excepts on paths a user sees: the expected error is still caught.

Cleanup round 10b replaced a broad ``except Exception`` with the errors the ``try`` really
raises. These measure the error each narrowed handler is for, so the page keeps answering
what it always answered.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from jinja2 import TemplateNotFound
from opi.utils.yaml_util import find_value_by_jsonpath
from opi.web.router_detail_edit import _get_wizard_token_with_body
from opi.web.router_wizard import service_help


class TestJsonPathThatDoesNotParse:
    def test_an_unparsable_path_yields_the_default(self):
        """A JSONPath the parser rejects yields the default, not an exception."""
        assert find_value_by_jsonpath({"name": "a"}, "$.[[[", default="fallback") == "fallback"

    def test_a_path_on_the_wrong_type_yields_the_default(self):
        assert find_value_by_jsonpath({"name": "a"}, "$.name[0].x", default=None) is None


class TestWizardTokenFromABrokenBody:
    @pytest.mark.asyncio
    async def test_unparsable_json_yields_no_token(self):
        """A body that is not JSON yields None, so the caller answers "session expired"."""
        request = MagicMock()
        request.query_params = {}
        request.headers = {"content-type": "application/json"}
        request.json = AsyncMock(side_effect=ValueError("no json"))

        assert await _get_wizard_token_with_body(request) is None

    @pytest.mark.asyncio
    async def test_an_unreadable_form_yields_no_token(self):
        request = MagicMock()
        request.query_params = {}
        request.headers = {"content-type": "application/x-www-form-urlencoded"}
        request.form = AsyncMock(side_effect=TypeError("broken form"))

        assert await _get_wizard_token_with_body(request) is None


class TestHelpTemplateThatDoesNotExist:
    @pytest.mark.asyncio
    async def test_a_missing_template_gives_404(self):
        """A help page that does not exist stays a 404 and not a 500."""
        request = MagicMock()
        request.url.path = "/forms/wizard/help/does-not-exist.md"
        with patch("opi.web.router_wizard.templates_lotc") as templates:
            templates.TemplateResponse.side_effect = TemplateNotFound("help/does-not-exist.md")
            with pytest.raises(HTTPException) as refusal:
                await service_help(request, "does-not-exist.md")

        assert refusal.value.status_code == 404
