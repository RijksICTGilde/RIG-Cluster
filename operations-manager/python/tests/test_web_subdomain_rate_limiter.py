"""
Tests for web subdomain check rate limiting.

Tests the rate limiting applied to the SSO-protected subdomain check endpoint.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

from opi.web.router_self_service import check_subdomain_availability_web, web_subdomain_check_rate_limiter
from starlette.requests import Request


class TestWebSubdomainCheckRateLimiter:
    """Tests for web subdomain check rate limiter."""

    def test_rate_limiter_is_configured(self):
        """web_subdomain_check_rate_limiter is properly configured."""
        # Verify the rate limiter instance exists and has expected settings
        assert web_subdomain_check_rate_limiter is not None
        # Same settings as API rate limiter: 30 requests/minute, burst of 10
        assert web_subdomain_check_rate_limiter.rate == 30 / 60.0  # 0.5 requests per second
        assert web_subdomain_check_rate_limiter.burst == 10

    def test_rate_limiter_allows_burst(self):
        """Rate limiter allows burst of requests."""
        # Use a unique client ID to avoid interference from other tests
        client_id = "test_web_client_burst_test"

        # Should allow burst of 10 requests
        for i in range(10):
            result = web_subdomain_check_rate_limiter.is_allowed(client_id)
            assert result is True, f"Request {i + 1} should be allowed within burst"

    def test_rate_limiter_blocks_after_burst(self):
        """Rate limiter blocks requests after burst is exhausted."""
        # Use a unique client ID to avoid interference from other tests
        client_id = "test_web_client_block_test"

        # Exhaust burst
        for _i in range(10):
            web_subdomain_check_rate_limiter.is_allowed(client_id)

        # 11th request should be blocked
        result = web_subdomain_check_rate_limiter.is_allowed(client_id)
        assert result is False, "Request after burst should be blocked"

    def test_rate_limiter_independent_from_api_limiter(self):
        """Web rate limiter is independent from API rate limiter."""
        from opi.api.router import subdomain_check_rate_limiter

        # They should be different instances
        assert web_subdomain_check_rate_limiter is not subdomain_check_rate_limiter

        # Use a unique client ID
        client_id = "test_independence_client"

        # Exhaust web limiter
        for _ in range(10):
            web_subdomain_check_rate_limiter.is_allowed(client_id)

        # API limiter should still allow (independent state)
        result = subdomain_check_rate_limiter.is_allowed(client_id)
        assert result is True, "API rate limiter should be independent from web rate limiter"


class TestWebSubdomainCheckAppliesReservedNamesPerDomain:
    """Dezelfde regel als in de wizard, op de check die het portaal live bevraagt."""

    @staticmethod
    async def _check(subdomain: str, base_domain: str) -> dict:
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/check-subdomain",
                "headers": [],
                "query_string": f"subdomain={subdomain}&base_domain={base_domain}".encode(),
                "client": ("10.0.0.1", 1234),
                "session": {"user": {"email": "dev@example.com"}},
            }
        )
        connector = MagicMock()
        connector.check_availability = AsyncMock(return_value=True)
        with (
            patch("opi.web.router_self_service.create_subdomain_connector", return_value=connector),
            patch.object(web_subdomain_check_rate_limiter, "is_allowed", return_value=True),
            patch("opi.core.config.settings.CLUSTER_MANAGER", "odcn-production"),
        ):
            response = await check_subdomain_availability_web(request)
        return json.loads(response.body)

    async def test_a_reserved_name_on_a_platform_domain_is_refused(self):
        body = await self._check("admin", "rijks.app")
        assert body["available"] is False
        assert body["validation_error"] == "Subdomein 'admin' is niet beschikbaar"

    async def test_the_same_name_on_a_tenant_domain_is_free(self):
        body = await self._check("admin", "uitbetrouwbarebron.nl")
        assert body["available"] is True
        assert body["validation_error"] is None
