"""
Tests for the IP rate limiter with time-based cleanup.

Tests the IPRateLimiter class that provides rate limiting for the
subdomain check endpoint with periodic memory cleanup.
"""

import time
from typing import Any
from unittest.mock import patch

import pytest
from opi.api.router import IPRateLimiter
from starlette.requests import Request


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
        for _i in range(5):
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

    def test_size_based_cleanup_triggers_on_ip_bucket_growth_too(self):
        """De drempel kijkt naar de grootste van de twee emmers, niet alleen de client-emmer.

        ``max_new_clients_per_minute=None`` (zoals ``API_FLOOD_LIMITER``) laat de emmer per
        IP meegroeien met elk verzonnen client-id. Kijkt de opruimdrempel alleen naar
        ``_tokens``, dan blijft die emmer per IP ongelimiteerd groeien terwijl ``_tokens``
        zelf klein blijft.
        """
        limiter = IPRateLimiter(
            requests_per_minute=30, burst=10, cleanup_interval=99999.0, max_new_clients_per_minute=None
        )

        for i in range(8001):
            ip = f"203.0.{i // 256}.{i % 256}"
            limiter._ip_tokens[ip] = 10.0
            limiter._ip_last_update[ip] = time.monotonic() - 5000

        assert len(limiter._tokens) == 0
        assert len(limiter._ip_tokens) > 8000

        limiter.is_allowed("10.0.0.1|fingerprint|")

        assert len(limiter._ip_tokens) < 10

    def test_gradual_purge_removes_stale_ip_entries_too(self):
        """De noodopruiming ruimt ook de emmer per IP op, niet alleen de emmer per client.

        Zonder dit raakt alleen ``_tokens`` begrensd door ``MAX_TRACKED_CLIENTS``, en groeit
        ``_ip_tokens`` ongelimiteerd door bij een stortvloed van verzonnen client-ids.
        """
        limiter = IPRateLimiter(requests_per_minute=30, burst=10)
        oud = time.monotonic() - (limiter.EMERGENCY_PURGE_MIN_AGE_SECONDS + 60)
        limiter._tokens["oude.client"] = 5.0
        limiter._last_update["oude.client"] = oud
        limiter._ip_tokens["203.0.113.1"] = 5.0
        limiter._ip_last_update["203.0.113.1"] = oud

        limiter._gradual_purge_unlocked()

        assert "oude.client" not in limiter._tokens
        assert "oude.client" not in limiter._last_update
        assert "203.0.113.1" not in limiter._ip_tokens
        assert "203.0.113.1" not in limiter._ip_last_update


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

        # Make requests from different IPs (stay under MAX_NEW_CLIENTS_PER_MINUTE=100)
        for i in range(80):
            limiter.is_allowed(f"192.168.{i // 256}.{i % 256}")

        # Force half the entries to be old
        old_time = time.monotonic() - 5000
        for ip in list(limiter._last_update.keys())[:40]:
            limiter._last_update[ip] = old_time

        # Ensure cleanup interval has passed
        limiter._last_cleanup = time.monotonic() - 1.0
        # Clear new-client timestamps so the next request isn't rate-limited
        limiter._new_client_timestamps.clear()

        # Trigger cleanup via new request
        limiter.is_allowed("10.0.0.1")

        # Should have cleaned up ~40 old entries, leaving ~40 recent + 1 new
        assert len(limiter._tokens) <= 45

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


class TestDeWeigeringBlijftGoedkoop:
    """De drie remmen uit commit ``6251026b7``, op de limiter zonder nieuwe-clientgrens.

    ``API_FLOOD_LIMITER`` staat op elk verzoek onder ``/api/`` en ``/auth/`` en draait
    zonder die grens, dus het client-id is van buiten te kiezen (de vingerafdruk is een
    hash van User-Agent, Accept-Language en Accept-Encoding). Zonder deze drie remmen
    bepaalt een aanvaller wat zijn eigen weigering kost, en daarmee wat een verzoek van
    iedere andere client kost.
    """

    @staticmethod
    def _limiter() -> IPRateLimiter:
        return IPRateLimiter(
            requests_per_minute=600, burst=10, cleanup_interval=99999.0, max_new_clients_per_minute=None
        )

    def test_een_geweigerd_verzoek_laat_geen_emmer_achter(self):
        """Verse client-ids vanaf een adres leveren niet meer emmers op dan de burst per IP.

        Voor de reparatie materialiseerde ``is_allowed`` de emmer voordat hij weigerde: deze
        500 verzoeken lieten er 500 achter.
        """
        limiter = self._limiter()

        for nummer in range(500):
            limiter.is_allowed(f"203.0.113.9|ua-{nummer}|")

        # De emmer per IP is het enige dat een verse client nog kan weigeren, dus zij
        # begrenst het aantal emmers. Bijvullen tijdens de lus geeft er een paar bij.
        assert len(limiter._last_update) <= limiter._per_ip_burst + 5
        assert len(limiter._tokens) == len(limiter._last_update)
        assert limiter.is_allowed("203.0.113.9|ua-nog-eentje|") is False

    def test_max_tracked_clients_is_een_harde_bovengrens(self, monkeypatch: pytest.MonkeyPatch):
        """Verdringing houdt beide emmersoorten op de grens, ook zonder iets om op te ruimen.

        Voor de reparatie was de grens alleen een drempel voor de opruiming, en die laat een
        emmer jonger dan een minuut staan: deze 5000 client-ids hielden er 5000 over.
        """
        monkeypatch.setattr(IPRateLimiter, "MAX_TRACKED_CLIENTS", 50)
        limiter = self._limiter()

        for nummer in range(5000):
            limiter.is_allowed(f"198.51.100.{nummer % 200}|ua-{nummer}|")

        assert len(limiter._last_update) == 50
        assert len(limiter._tokens) == 50
        assert len(limiter._ip_last_update) <= 50
        assert len(limiter._ip_tokens) == len(limiter._ip_last_update)

    def test_de_noodopruiming_draait_hoogstens_een_keer_per_interval(self, monkeypatch: pytest.MonkeyPatch):
        """Anders draaien er twee O(n)-scans per verzoek, onder de lock van elke client.

        Voor de reparatie draaide de scan 95 keer in deze 100 verzoeken.
        """
        monkeypatch.setattr(IPRateLimiter, "CLEANUP_THRESHOLD", 5)
        limiter = self._limiter()
        scans = 0
        echte_scan = limiter._cleanup_old_entries_unlocked

        def tel(max_age_seconds: int = 600) -> None:
            nonlocal scans
            scans += 1
            echte_scan(max_age_seconds)

        monkeypatch.setattr(limiter, "_cleanup_old_entries_unlocked", tel)

        for nummer in range(100):
            limiter.is_allowed(f"203.0.113.9|ua-{nummer}|")

        assert scans == 1


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

            result = await connector.get_resources_by_label("ingress", namespace="test-ns", label_selector="app=test")

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

            result = await connector.get_resources_by_label("ingress", namespace="test-ns", label_selector="app=test")

            assert result == []


def _request(peer: str, headers: dict[str, str] | None = None, sessie: dict[str, str] | None = None) -> Request:
    """Een echt Request-object: het client-id leest de ruwe headerregels, geen dict."""
    scope: dict[str, Any] = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/ping",
        "query_string": b"",
        "root_path": "",
        "server": ("testserver", 80),
        "client": (peer, 1234),
        "headers": [(naam.encode(), waarde.encode()) for naam, waarde in (headers or {}).items()],
    }
    if sessie is not None:
        scope["session"] = sessie
    return Request(scope)


class TestIPRateLimiterClientIdentification:
    """Hoe een client wordt geidentificeerd. Het IP zelf: tests/test_client_ip.py."""

    def test_get_client_fingerprint_generates_hash(self):
        """get_client_fingerprint generates consistent hash from headers."""
        from unittest.mock import MagicMock

        request = MagicMock()
        request.headers = {
            "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
            "accept-language": "en-US,en;q=0.9",
            "accept-encoding": "gzip, deflate, br",
        }

        fingerprint = IPRateLimiter.get_client_fingerprint(request)

        # Should be a 16-character hex string
        assert len(fingerprint) == 16
        assert all(c in "0123456789abcdef" for c in fingerprint)

        # Same headers should produce same fingerprint
        fingerprint2 = IPRateLimiter.get_client_fingerprint(request)
        assert fingerprint == fingerprint2

    def test_get_client_fingerprint_different_for_different_headers(self):
        """get_client_fingerprint produces different hashes for different headers."""
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

        fp1 = IPRateLimiter.get_client_fingerprint(request1)
        fp2 = IPRateLimiter.get_client_fingerprint(request2)

        assert fp1 != fp2  # Different user agents should produce different fingerprints

    def test_get_client_identifier_combines_factors(self):
        """get_client_identifier combines IP, fingerprint, and session."""
        request = _request(
            "203.0.113.50",
            headers={
                "user-agent": "TestAgent/1.0",
                "accept-language": "en",
                "accept-encoding": "gzip",
            },
            sessie={"_session_id": "abc123def456"},
        )

        identifier = IPRateLimiter.get_client_identifier(request)

        # Should contain IP, fingerprint, and session
        parts = identifier.split("|")
        assert len(parts) == 3
        assert parts[0] == "203.0.113.50"  # IP
        assert len(parts[1]) == 16  # Fingerprint hash
        assert parts[2] == "abc123def456"[:32]  # Session (truncated)

    def test_get_client_identifier_without_session(self):
        """get_client_identifier works without session (unauthenticated)."""
        request = _request(
            "192.168.1.1",
            headers={
                "user-agent": "TestAgent/1.0",
                "accept-language": "en",
                "accept-encoding": "gzip",
            },
        )

        identifier = IPRateLimiter.get_client_identifier(request)

        parts = identifier.split("|")
        assert len(parts) == 3
        assert parts[0] == "192.168.1.1"
        assert len(parts[1]) == 16
        assert parts[2] == ""  # Empty session

    def test_spoofed_xff_levert_hetzelfde_client_id(self):
        """Een verzonnen X-Forwarded-For geeft geen eigen emmer meer.

        Deze test pinde tot RC-368 het omgekeerde vast: de linkse headerwaarde werd het
        client-id, dus wie zijn adres verzon koos zijn eigen emmer. Het IP komt nu van
        ``client_ip``, en zonder vertrouwde proxy-hop telt dat op het peer-adres.
        """
        headers = {
            "user-agent": "Same Browser/1.0",
            "accept-language": "en-US",
            "accept-encoding": "gzip",
        }
        ids = {
            IPRateLimiter.get_client_identifier(_request("127.0.0.1", headers={**headers, "x-forwarded-for": ip}))
            for ip in ("1.1.1.1", "2.2.2.2", "3.3.3.3")
        }

        assert len(ids) == 1
        assert next(iter(ids)).startswith("127.0.0.1|")


class TestIPRateLimiterRetention:
    """Een emmer blijft minstens zijn vulvenster staan (RC-313)."""

    def test_een_uurgrens_overleeft_het_opruimen(self):
        """Het opruimen gooide elke emmer weg die 600 seconden niet was aangeraakt.

        Een weggegooide emmer begint op de volle burst, dus een grens van 20 per uur hield
        geen uur: elf minuten stil, een verzoek van een ander client-id dat het opruimen
        opwekt, en er konden opnieuw 20 verzoeken in hetzelfde uur.
        """
        limiter = IPRateLimiter(requests_per_minute=20 / 60, burst=20)  # 20 per uur
        client = "10.0.0.1|fingerprint|"

        for _ in range(20):
            assert limiter.is_allowed(client) is True
        assert limiter.is_allowed(client) is False

        # Elf minuten later: ouder dan de 600 seconden van het opruimen, binnen het uur.
        now = time.monotonic()
        limiter._last_update[client] = now - 660
        limiter._ip_last_update["10.0.0.1"] = now - 660
        limiter._last_cleanup = now - limiter._cleanup_interval - 1
        limiter._new_client_timestamps.clear()

        limiter.is_allowed("10.0.0.2|fingerprint|")  # ander client-id, wekt het opruimen op

        assert client in limiter._last_update
        assert limiter._tokens[client] < limiter.burst

    def test_een_grens_per_minuut_wordt_nog_wel_opgeruimd(self):
        """De ondergrens is het vulvenster, dus een korte grens ruimt op zoals eerst."""
        limiter = IPRateLimiter(requests_per_minute=30, burst=10)
        limiter._tokens["oud.ip"] = 0.0
        limiter._last_update["oud.ip"] = time.monotonic() - 700

        limiter.cleanup_old_entries()

        assert "oud.ip" not in limiter._last_update
