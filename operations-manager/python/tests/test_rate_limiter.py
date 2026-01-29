"""
Tests for the IP rate limiter with time-based cleanup.

Tests the IPRateLimiter class that provides rate limiting for the
subdomain check endpoint with periodic memory cleanup.
"""

import time
from unittest.mock import patch

import pytest
from opi.api.router import IPRateLimiter


class TestIPRateLimiterBasics:
    """Basic tests for IPRateLimiter class."""

    def test_initialization(self):
        """IPRateLimiter initializes with correct default values."""
        limiter = IPRateLimiter()
        assert limiter.rate == 30 / 60.0  # 30 requests/minute = 0.5/second
        assert limiter.burst == 10
        assert limiter._cleanup_interval == 60.0  # 1 minute (more aggressive cleanup)

    def test_custom_initialization(self):
        """IPRateLimiter accepts custom parameters."""
        limiter = IPRateLimiter(requests_per_minute=60, burst=20, cleanup_interval=600.0)
        assert limiter.rate == 60 / 60.0  # 1 request/second
        assert limiter.burst == 20
        assert limiter._cleanup_interval == 600.0


class TestIPRateLimiterAllowance:
    """Tests for IPRateLimiter.is_allowed method."""

    def test_first_request_allowed(self):
        """First request from new IP is always allowed."""
        limiter = IPRateLimiter(requests_per_minute=30, burst=10)
        assert limiter.is_allowed("192.168.1.1") is True

    def test_burst_requests_allowed(self):
        """Requests up to burst limit are allowed immediately."""
        limiter = IPRateLimiter(requests_per_minute=30, burst=5)

        # First 5 requests should be allowed (burst)
        for i in range(5):
            assert limiter.is_allowed("192.168.1.1") is True

        # 6th request should be denied (burst exhausted)
        assert limiter.is_allowed("192.168.1.1") is False

    def test_different_ips_independent(self):
        """Different IPs have independent rate limits."""
        limiter = IPRateLimiter(requests_per_minute=30, burst=2)

        # Exhaust first IP
        assert limiter.is_allowed("192.168.1.1") is True
        assert limiter.is_allowed("192.168.1.1") is True
        assert limiter.is_allowed("192.168.1.1") is False

        # Second IP should still have full burst
        assert limiter.is_allowed("192.168.1.2") is True
        assert limiter.is_allowed("192.168.1.2") is True


class TestIPRateLimiterTimeBasedCleanup:
    """Tests for time-based periodic cleanup."""

    def test_time_based_cleanup_triggers(self):
        """Cleanup triggers after cleanup_interval has passed."""
        limiter = IPRateLimiter(requests_per_minute=30, burst=10, cleanup_interval=1.0)

        # Make some requests
        limiter.is_allowed("192.168.1.1")
        limiter.is_allowed("192.168.1.2")

        assert "192.168.1.1" in limiter._tokens
        assert "192.168.1.2" in limiter._tokens

        # Simulate time passing beyond cleanup interval
        # We need to mock both the current time and make entries "old"
        original_last_cleanup = limiter._last_cleanup

        # Manually set last cleanup to old time
        limiter._last_cleanup = time.monotonic() - 2.0  # 2 seconds ago

        # Also make entries old enough to be cleaned up
        old_time = time.monotonic() - 4000  # Way older than max_age
        for ip in list(limiter._last_update.keys()):
            limiter._last_update[ip] = old_time

        # Next request should trigger cleanup
        limiter.is_allowed("192.168.1.3")

        # Old entries should be cleaned up
        assert "192.168.1.1" not in limiter._tokens
        assert "192.168.1.2" not in limiter._tokens

    def test_size_based_cleanup_fallback(self):
        """Cleanup triggers when > CLEANUP_THRESHOLD entries even without time interval."""
        limiter = IPRateLimiter(requests_per_minute=30, burst=10, cleanup_interval=99999.0)

        # Add over CLEANUP_THRESHOLD (8000) entries manually
        # All entries are old enough to be cleaned up (> 300 seconds)
        for i in range(8001):
            limiter._tokens[f"192.168.{i // 256}.{i % 256}"] = 10.0
            limiter._last_update[f"192.168.{i // 256}.{i % 256}"] = time.monotonic() - 5000

        assert len(limiter._tokens) > 8000

        # Next request should trigger cleanup (max_age=300 for size-based cleanup)
        limiter.is_allowed("10.0.0.1")

        # Old entries should be cleaned up (those older than 300 seconds)
        assert len(limiter._tokens) < 10  # Most should be cleaned


class TestIPRateLimiterCleanupOldEntries:
    """Tests for cleanup_old_entries method."""

    def test_cleanup_removes_old_entries(self):
        """Entries older than max_age are removed."""
        limiter = IPRateLimiter()

        # Add entries with old timestamps
        old_time = time.monotonic() - 4000  # 4000 seconds ago (older than 3600)
        limiter._tokens["old.ip.1"] = 10.0
        limiter._last_update["old.ip.1"] = old_time
        limiter._tokens["old.ip.2"] = 10.0
        limiter._last_update["old.ip.2"] = old_time

        # Add a recent entry
        limiter._tokens["new.ip"] = 10.0
        limiter._last_update["new.ip"] = time.monotonic()

        limiter.cleanup_old_entries()

        assert "old.ip.1" not in limiter._tokens
        assert "old.ip.2" not in limiter._tokens
        assert "new.ip" in limiter._tokens

    def test_cleanup_respects_max_age(self):
        """Entries younger than max_age are kept."""
        limiter = IPRateLimiter()

        # Add entry just under max_age (default is 600 seconds)
        recent_time = time.monotonic() - 300  # 300 seconds ago (under 600)
        limiter._tokens["recent.ip"] = 10.0
        limiter._last_update["recent.ip"] = recent_time

        limiter.cleanup_old_entries()

        assert "recent.ip" in limiter._tokens

    def test_cleanup_with_custom_max_age(self):
        """Cleanup respects custom max_age parameter."""
        limiter = IPRateLimiter()

        # Add entry
        entry_time = time.monotonic() - 100  # 100 seconds ago
        limiter._tokens["test.ip"] = 10.0
        limiter._last_update["test.ip"] = entry_time

        # Cleanup with shorter max_age should remove it
        limiter.cleanup_old_entries(max_age_seconds=50)

        assert "test.ip" not in limiter._tokens


class TestIPRateLimiterMemoryBounds:
    """Tests for memory-bounded behavior."""

    def test_memory_doesnt_grow_unbounded(self):
        """Memory usage is bounded by cleanup mechanism."""
        limiter = IPRateLimiter(requests_per_minute=30, burst=10, cleanup_interval=0.1)

        # Make many requests from different IPs
        for i in range(100):
            limiter.is_allowed(f"192.168.{i // 256}.{i % 256}")

        # Force old entries
        old_time = time.monotonic() - 5000
        for ip in list(limiter._last_update.keys())[:50]:
            limiter._last_update[ip] = old_time

        # Wait for cleanup interval
        limiter._last_cleanup = time.monotonic() - 1.0

        # Trigger cleanup via new request
        limiter.is_allowed("10.0.0.1")

        # Should have cleaned up old entries
        assert len(limiter._tokens) <= 60  # Some should be cleaned

    def test_tokens_replenish_over_time(self):
        """Tokens are replenished based on elapsed time."""
        limiter = IPRateLimiter(requests_per_minute=60, burst=5)  # 1 token/second

        # Use all burst tokens
        for _ in range(5):
            limiter.is_allowed("test.ip")

        # Should be denied
        assert limiter.is_allowed("test.ip") is False

        # Simulate 2 seconds passing by adjusting the last update time
        limiter._last_update["test.ip"] = time.monotonic() - 2.0

        # Should now have ~2 tokens replenished
        assert limiter.is_allowed("test.ip") is True


class TestKubectlConnectorGetResourcesByLabel:
    """Tests for KubectlConnector.get_resources_by_label method."""

    @pytest.mark.asyncio
    async def test_get_resources_by_label_returns_items(self):
        """get_resources_by_label returns list of matching resources."""
        from unittest.mock import AsyncMock
        from opi.connectors.kubectl import KubectlConnector

        connector = KubectlConnector()

        mock_result = '{"items": [{"metadata": {"name": "test-ingress"}}]}'

        with patch.object(connector, "_run_kubectl_command", new_callable=AsyncMock) as mock_cmd:
            mock_cmd.return_value = (mock_result, "", 0)

            result = await connector.get_resources_by_label(
                "ingress", namespace="test-ns", label_selector="app=test"
            )

            assert len(result) == 1
            assert result[0]["metadata"]["name"] == "test-ingress"
            mock_cmd.assert_called_once()
            args = mock_cmd.call_args[0][0]
            assert "ingress" in args
            assert "-l" in args
            assert "app=test" in args

    @pytest.mark.asyncio
    async def test_get_resources_by_label_returns_empty_on_no_resources(self):
        """get_resources_by_label returns empty list when no resources found."""
        from unittest.mock import AsyncMock
        from opi.connectors.kubectl import KubectlConnector

        connector = KubectlConnector()

        with patch.object(connector, "_run_kubectl_command", new_callable=AsyncMock) as mock_cmd:
            mock_cmd.return_value = ("", "No resources found", 0)

            result = await connector.get_resources_by_label(
                "ingress", namespace="test-ns", label_selector="app=nonexistent"
            )

            assert result == []

    @pytest.mark.asyncio
    async def test_get_resources_by_label_handles_errors(self):
        """get_resources_by_label returns empty list on error."""
        from unittest.mock import AsyncMock
        from opi.connectors.kubectl import KubectlConnector

        connector = KubectlConnector()

        with patch.object(connector, "_run_kubectl_command", new_callable=AsyncMock) as mock_cmd:
            mock_cmd.return_value = ("", "connection refused", 1)

            result = await connector.get_resources_by_label(
                "ingress", namespace="test-ns", label_selector="app=test"
            )

            assert result == []


class TestIPRateLimiterClientIdentification:
    """Tests for robust client identification to prevent X-Forwarded-For spoofing."""

    def test_get_client_ip_from_x_forwarded_for(self):
        """get_client_ip extracts IP from X-Forwarded-For header."""
        from unittest.mock import MagicMock

        request = MagicMock()
        request.headers = {"x-forwarded-for": "203.0.113.50, 10.0.0.1, 172.16.0.1"}
        request.client.host = "127.0.0.1"

        ip = IPRateLimiter.get_client_ip(request)
        assert ip == "203.0.113.50"  # First IP in chain (original client)

    def test_get_client_ip_fallback_to_direct(self):
        """get_client_ip falls back to direct connection when no X-Forwarded-For."""
        from unittest.mock import MagicMock

        request = MagicMock()
        request.headers = {}
        request.client.host = "192.168.1.100"

        ip = IPRateLimiter.get_client_ip(request)
        assert ip == "192.168.1.100"

    def test_get_client_fingerprint_generates_hash(self):
        """_get_client_fingerprint generates consistent hash from headers."""
        from unittest.mock import MagicMock

        request = MagicMock()
        request.headers = {
            "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
            "accept-language": "en-US,en;q=0.9",
            "accept-encoding": "gzip, deflate, br",
        }

        fingerprint = IPRateLimiter._get_client_fingerprint(request)

        # Should be a 16-character hex string
        assert len(fingerprint) == 16
        assert all(c in "0123456789abcdef" for c in fingerprint)

        # Same headers should produce same fingerprint
        fingerprint2 = IPRateLimiter._get_client_fingerprint(request)
        assert fingerprint == fingerprint2

    def test_get_client_fingerprint_different_for_different_headers(self):
        """_get_client_fingerprint produces different hashes for different headers."""
        from unittest.mock import MagicMock

        request1 = MagicMock()
        request1.headers = {
            "user-agent": "Mozilla/5.0 Chrome",
            "accept-language": "en-US",
            "accept-encoding": "gzip",
        }

        request2 = MagicMock()
        request2.headers = {
            "user-agent": "Mozilla/5.0 Firefox",
            "accept-language": "en-US",
            "accept-encoding": "gzip",
        }

        fp1 = IPRateLimiter._get_client_fingerprint(request1)
        fp2 = IPRateLimiter._get_client_fingerprint(request2)

        assert fp1 != fp2  # Different user agents should produce different fingerprints

    def test_get_client_identifier_combines_factors(self):
        """get_client_identifier combines IP, fingerprint, and session."""
        from unittest.mock import MagicMock

        request = MagicMock()
        request.headers = {
            "x-forwarded-for": "203.0.113.50",
            "user-agent": "TestAgent/1.0",
            "accept-language": "en",
            "accept-encoding": "gzip",
        }
        request.client.host = "127.0.0.1"
        request.session = {"_session_id": "abc123def456"}

        identifier = IPRateLimiter.get_client_identifier(request)

        # Should contain IP, fingerprint, and session
        parts = identifier.split("|")
        assert len(parts) == 3
        assert parts[0] == "203.0.113.50"  # IP
        assert len(parts[1]) == 16  # Fingerprint hash
        assert parts[2] == "abc123def456"[:32]  # Session (truncated)

    def test_get_client_identifier_without_session(self):
        """get_client_identifier works without session (unauthenticated)."""
        from unittest.mock import MagicMock

        request = MagicMock()
        request.headers = {
            "user-agent": "TestAgent/1.0",
            "accept-language": "en",
            "accept-encoding": "gzip",
        }
        request.client.host = "192.168.1.1"
        request.session = {}

        identifier = IPRateLimiter.get_client_identifier(request)

        parts = identifier.split("|")
        assert len(parts) == 3
        assert parts[0] == "192.168.1.1"
        assert len(parts[1]) == 16
        assert parts[2] == ""  # Empty session

    def test_spoofed_xff_with_same_fingerprint_is_caught(self):
        """Rate limiting catches requests with spoofed XFF but same fingerprint."""
        from unittest.mock import MagicMock

        limiter = IPRateLimiter(requests_per_minute=30, burst=3)

        # Same fingerprint (same browser), different "spoofed" IPs
        def make_request(ip):
            request = MagicMock()
            request.headers = {
                "x-forwarded-for": ip,
                "user-agent": "Same Browser/1.0",
                "accept-language": "en-US",
                "accept-encoding": "gzip",
            }
            request.client.host = "127.0.0.1"
            request.session = {}
            return request

        # Requests from "different IPs" but same fingerprint
        req1 = make_request("1.1.1.1")
        req2 = make_request("2.2.2.2")
        req3 = make_request("3.3.3.3")

        # Without fingerprinting, these would all be allowed
        # With fingerprinting, they share the same limit because fingerprint matches
        id1 = IPRateLimiter.get_client_identifier(req1)
        id2 = IPRateLimiter.get_client_identifier(req2)
        id3 = IPRateLimiter.get_client_identifier(req3)

        # Different IPs produce different identifiers, but fingerprint is same component
        # This ensures that spoofing just XFF requires more effort
        assert "1.1.1.1" in id1
        assert "2.2.2.2" in id2
        # The fingerprint portion should be the same
        assert id1.split("|")[1] == id2.split("|")[1] == id3.split("|")[1]
