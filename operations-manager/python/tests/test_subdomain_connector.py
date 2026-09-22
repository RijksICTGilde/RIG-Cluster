"""
Tests for the subdomain registry connector.

Tests the SubdomainConnector class that manages globally unique subdomains
for the nice URL feature.
"""

from typing import ClassVar
from unittest.mock import AsyncMock, patch

import pytest
from opi.connectors.subdomain import (
    BARE_DOMAIN_SUBDOMAIN,
    RESERVED_SUBDOMAINS,
    SubdomainNotAvailableError,
    SubdomainValidationError,
    find_deployments_for_domain_item,
    validate_subdomain,
    validate_subdomain_for_domain,
)
from opi.services.persistence.subdomain_registry import (
    SUBDOMAIN_REGISTRY_TABLE_SQL,
    SubdomainConnector,
    create_subdomain_connector,
)


class TestSubdomainConnectorBasics:
    """Basic tests for SubdomainConnector class."""

    def test_create_subdomain_connector(self):
        """create_subdomain_connector returns a SubdomainConnector instance."""
        connector = create_subdomain_connector()
        assert isinstance(connector, SubdomainConnector)

    def test_table_sql_creates_table(self):
        """SUBDOMAIN_REGISTRY_TABLE_SQL contains CREATE TABLE statement."""
        assert "CREATE TABLE IF NOT EXISTS subdomain_registry" in SUBDOMAIN_REGISTRY_TABLE_SQL

    def test_table_sql_creates_indexes(self):
        """SUBDOMAIN_REGISTRY_TABLE_SQL contains index creation."""
        assert "CREATE INDEX IF NOT EXISTS idx_subdomain_project" in SUBDOMAIN_REGISTRY_TABLE_SQL
        assert "CREATE INDEX IF NOT EXISTS idx_subdomain_deployment" in SUBDOMAIN_REGISTRY_TABLE_SQL

    def test_table_sql_has_unique_constraint(self):
        """SUBDOMAIN_REGISTRY_TABLE_SQL has unique constraint on subdomain+base_domain."""
        assert "UNIQUE (subdomain, base_domain)" in SUBDOMAIN_REGISTRY_TABLE_SQL


class TestValidateSubdomain:
    """Tests for validate_subdomain function."""

    def test_valid_subdomain(self):
        """Valid subdomains pass validation."""
        valid_subdomains = ["myapp", "my-app", "app123", "a", "abc123def"]
        for subdomain in valid_subdomains:
            is_valid, error = validate_subdomain(subdomain)
            assert is_valid is True, f"'{subdomain}' should be valid: {error}"
            assert error is None

    def test_empty_subdomain(self):
        """Empty subdomain fails validation."""
        is_valid, error = validate_subdomain("")
        assert is_valid is False
        assert "leeg" in error.lower()  # Dutch: "mag niet leeg zijn"

    def test_too_long_subdomain(self):
        """Subdomain exceeding 63 characters fails validation."""
        long_subdomain = "a" * 64
        is_valid, error = validate_subdomain(long_subdomain)
        assert is_valid is False
        assert "63" in error

    def test_hyphen_at_start(self):
        """Subdomain starting with hyphen fails validation."""
        is_valid, error = validate_subdomain("-myapp")
        assert is_valid is False
        assert "beginnen" in error.lower()  # Dutch: "mag niet beginnen met"

    def test_hyphen_at_end(self):
        """Subdomain ending with hyphen fails validation."""
        is_valid, error = validate_subdomain("myapp-")
        assert is_valid is False
        assert "eindigen" in error.lower()  # Dutch: "mag niet eindigen met"

    def test_invalid_characters(self):
        """Subdomain with invalid characters fails validation."""
        # Note: uppercase letters are lowercased before validation, so MY-APP becomes my-app which is valid
        invalid_subdomains = ["my_app", "my.app", "my app"]
        for subdomain in invalid_subdomains:
            is_valid, error = validate_subdomain(subdomain)
            assert is_valid is False, f"'{subdomain}' should be invalid"

    def test_uppercase_is_lowercased(self):
        """Uppercase letters are lowercased and accepted."""
        is_valid, error = validate_subdomain("MY-APP")
        assert is_valid is True  # Gets lowercased to my-app which is valid

    def test_a_reserved_name_passes_the_form_rules(self):
        """De vormcontrole kent het basisdomein niet en oordeelt dus niet over reservering.

        ``www`` is een geldige DNS-naam. Of hij mag, hangt af van het domein, en dat is wat
        ``validate_subdomain_for_domain`` erbij haalt.
        """
        for subdomain in ["www", "api", "admin", "mail", "test"]:
            is_valid, error = validate_subdomain(subdomain)
            assert is_valid is True, f"'{subdomain}' is een geldige DNS-naam: {error}"

    def test_reserved_subdomains_are_defined(self):
        """RESERVED_SUBDOMAINS contains expected entries."""
        assert "www" in RESERVED_SUBDOMAINS
        assert "api" in RESERVED_SUBDOMAINS
        assert "admin" in RESERVED_SUBDOMAINS
        assert len(RESERVED_SUBDOMAINS) > 50  # Reasonable number of reserved names


class TestValidateSubdomainForDomain:
    """De reserveringslijst geldt op onze eigen zones, niet op het domein van een tenant.

    De aanleiding: ubbw-0i1 vroeg ``test.uitbetrouwbarebron.nl`` aan, zijn EIGEN domein, en
    kreeg tweemaal "niet beschikbaar" omdat ``test`` in de lijst staat. Aan de platformkant
    beschermt die weigering niets: wij zetten geen enkele hostnaam in de zone van die tenant.
    """

    @pytest.mark.parametrize("subdomain", ["admin", "www", "api", "test"])
    @pytest.mark.parametrize("base_domain", ["rijks.app", "rijksapp.nl", "rijksapp.dev"])
    def test_reserved_names_stay_refused_on_a_platform_domain(self, subdomain, base_domain):
        is_valid, error = validate_subdomain_for_domain(subdomain, base_domain, "odcn-production")
        assert is_valid is False, f"'{subdomain}.{base_domain}' hoort geweigerd te blijven"
        assert error == f"Subdomein '{subdomain}' is niet beschikbaar"

    def test_the_message_stays_generic(self):
        """Zonder het woord "gereserveerd" en niet te onderscheiden van "al in gebruik":
        wie die tekst specifieker maakt, maakt de lijst aftastbaar."""
        _, error = validate_subdomain_for_domain("admin", "rijks.app", "odcn-production")
        assert "gereserveerd" not in error.lower()
        assert "reserved" not in error.lower()

    @pytest.mark.parametrize("subdomain", ["admin", "www", "api", "test"])
    def test_reserved_names_are_allowed_on_a_tenant_domain(self, subdomain):
        is_valid, error = validate_subdomain_for_domain(subdomain, "uitbetrouwbarebron.nl", "odcn-production")
        assert is_valid is True, f"'{subdomain}.uitbetrouwbarebron.nl' is hun zone: {error}"
        assert error is None

    def test_a_subzone_of_a_platform_domain_keeps_the_list(self):
        """``team.rijks.app`` staat in geen enkele lijst, maar ``admin`` is er een label op
        ONZE registreerbare zone. Dit valt om bij een controle op lidmaatschap."""
        is_valid, _ = validate_subdomain_for_domain("admin", "team.rijks.app", "odcn-production")
        assert is_valid is False

    def test_the_cluster_postfix_zone_keeps_the_list(self):
        """Valt om als de beheerlijst die zone niet noemt."""
        is_valid, _ = validate_subdomain_for_domain("admin", "rig.prd1.gn2.quattro.rijksapps.nl", "odcn-production")
        assert is_valid is False

    def test_a_tenant_domain_under_the_postfix_parent_keeps_its_own_names(self):
        """ug-zxt publiceert op ``ux-onderzoeken.rijksapps.nl``. Valt om bij een beheerlijst
        die de ouder ``rijksapps.nl`` noemt in plaats van de postfix-zone zelf."""
        is_valid, error = validate_subdomain_for_domain("admin", "ux-onderzoeken.rijksapps.nl", "odcn-production")
        assert is_valid is True, error

    def test_the_form_rules_still_apply_on_a_tenant_domain(self):
        """De vormregels zijn domeinonafhankelijk en gaan niet mee de deur uit."""
        for subdomain, fragment in [("", "leeg"), ("a" * 64, "63"), ("-x", "beginnen"), ("x-", "eindigen")]:
            is_valid, error = validate_subdomain_for_domain(subdomain, "uitbetrouwbarebron.nl", "odcn-production")
            assert is_valid is False, f"'{subdomain}' hoort ongeldig te zijn"
            assert fragment in error.lower()

    def test_uppercase_is_lowercased_and_valid(self):
        is_valid, error = validate_subdomain_for_domain("MY-APP", "uitbetrouwbarebron.nl", "odcn-production")
        assert is_valid is True, error

    def test_the_cluster_decides_which_zones_are_ours(self):
        """Op sandboxed-local is robbertuittenbroek.nl WEL van ons, dus geldt de lijst daar.
        Dat is de clusterconfig die zijn werk doet."""
        assert validate_subdomain_for_domain("admin", "robbertuittenbroek.nl", "sandboxed-local")[0] is False
        assert validate_subdomain_for_domain("admin", "robbertuittenbroek.nl", "odcn-production")[0] is True


class TestSubdomainValidationInRegister:
    """Tests for validation integration in SubdomainConnector.register."""

    @pytest.mark.asyncio
    async def test_register_validates_subdomain_format(self):
        """register raises SubdomainValidationError for invalid subdomain."""
        connector = SubdomainConnector()

        with pytest.raises(SubdomainValidationError) as exc_info:
            await connector.register(
                subdomain="-invalid",
                base_domain="rijks.app",
                project_name="my-project",
                deployment_name="prod",
                cluster="local",
            )

        assert "beginnen" in str(exc_info.value).lower()  # Dutch: "mag niet beginnen met"

    @pytest.mark.asyncio
    async def test_register_rejects_reserved_subdomain_on_a_platform_domain(self):
        """register raises SubdomainValidationError for reserved subdomain.

        Domein en cluster horen bij elkaar: op ``local`` is ``rijks.app`` geen zone van ons,
        en dan legt deze toets het verkeerde gedrag vast.

        NOTE: Error message is intentionally generic to prevent enumeration attacks.
        """
        connector = SubdomainConnector()

        with pytest.raises(SubdomainValidationError) as exc_info:
            await connector.register(
                subdomain="www",
                base_domain="rijks.app",
                project_name="my-project",
                deployment_name="prod",
                cluster="odcn-production",
            )

        # Generic error message - no "gereserveerd" to prevent enumeration
        assert "niet beschikbaar" in str(exc_info.value).lower()  # Dutch: "is niet beschikbaar"

    @pytest.mark.asyncio
    async def test_register_lets_a_reserved_name_through_on_a_tenant_domain(self):
        """Het publicatiepad is de andere helft: repareer je alleen het formulier, dan komt
        de naam door de wizard en klapt het uitrollen alsnog hier.

        De naam komt voorbij de validatie en strandt pas op de beschikbaarheidscheck, die
        hier nee zegt. Die andere uitzondering is juist het bewijs: de reservering bijt niet
        meer, de check erna wel.
        """
        connector = SubdomainConnector()

        with (
            patch.object(SubdomainConnector, "check_availability", AsyncMock(return_value=False)),
            pytest.raises(SubdomainNotAvailableError),
        ):
            await connector.register(
                subdomain="test",
                base_domain="uitbetrouwbarebron.nl",
                project_name="ubbw-0i1",
                deployment_name="prod",
                cluster="odcn-production",
            )

    @pytest.mark.asyncio
    async def test_register_or_update_lets_a_reserved_name_through_on_a_tenant_domain(self):
        """De tweede ingang van het publicatiepad, dezelfde meting."""
        connector = SubdomainConnector()

        with (
            patch.object(SubdomainConnector, "get_by_deployment", AsyncMock(return_value=None)),
            patch.object(SubdomainConnector, "check_availability", AsyncMock(return_value=False)),
            pytest.raises(SubdomainNotAvailableError),
        ):
            await connector.register_or_update_for_deployment(
                subdomain="test",
                base_domain="uitbetrouwbarebron.nl",
                project_name="ubbw-0i1",
                deployment_name="prod",
                cluster="odcn-production",
            )

    @pytest.mark.asyncio
    async def test_register_or_update_still_refuses_a_reserved_name_on_a_platform_domain(self):
        connector = SubdomainConnector()

        with pytest.raises(SubdomainValidationError) as exc_info:
            await connector.register_or_update_for_deployment(
                subdomain="admin",
                base_domain="rijks.app",
                project_name="my-project",
                deployment_name="prod",
                cluster="odcn-production",
            )

        assert "niet beschikbaar" in str(exc_info.value).lower()


class TestValidateBaseDomain:
    """Tests for validate_base_domain function."""

    def test_valid_base_domain_local(self):
        """Valid base domain for local cluster passes validation."""
        from opi.connectors.subdomain import validate_base_domain

        is_valid, error = validate_base_domain("kind", "local")
        assert is_valid is True
        assert error is None

    def test_valid_base_domain_production(self):
        """Valid base domain for production cluster passes validation."""
        from opi.connectors.subdomain import validate_base_domain

        is_valid, error = validate_base_domain("rijks.app", "odcn-production")
        assert is_valid is True
        assert error is None

    def test_invalid_base_domain(self):
        """Invalid base domain fails validation (must be syntactically invalid)."""
        from opi.connectors.subdomain import validate_base_domain

        # Custom domains with valid syntax are now accepted, so use a truly invalid domain
        is_valid, error = validate_base_domain("-invalid", "local")
        assert is_valid is False
        assert "ondersteund" in error.lower()  # Dutch: "geen ondersteund base domain"

    def test_empty_base_domain(self):
        """Empty base domain fails validation."""
        from opi.connectors.subdomain import validate_base_domain

        is_valid, error = validate_base_domain("", "local")
        assert is_valid is False
        assert "leeg" in error.lower()  # Dutch: "mag niet leeg zijn"

    def test_case_insensitive(self):
        """Base domain validation is case insensitive."""
        from opi.connectors.subdomain import validate_base_domain

        is_valid, error = validate_base_domain("RIJKS.APP", "odcn-production")
        assert is_valid is True
        assert error is None

    def test_all_clusters_validation(self):
        """Base domain validation without cluster checks against all supported domains."""
        from opi.connectors.subdomain import validate_base_domain

        # rijks.app should be valid when no cluster specified
        is_valid, error = validate_base_domain("rijks.app")
        assert is_valid is True

        # kind should be valid when no cluster specified
        is_valid, error = validate_base_domain("kind")
        assert is_valid is True


class TestGetSupportedBaseDomains:
    """Tests for get_supported_base_domains function."""

    def test_local_cluster_domains(self):
        """get_supported_base_domains returns correct domains for local cluster."""
        from opi.connectors.subdomain import get_supported_base_domains

        domains = get_supported_base_domains("local")
        assert "kind" in domains
        assert "local" in domains

    def test_production_cluster_domains(self):
        """get_supported_base_domains returns correct domains for production cluster."""
        from opi.connectors.subdomain import get_supported_base_domains

        domains = get_supported_base_domains("odcn-production")
        assert "rijks.app" in domains
        assert "rijksapp.dev" in domains
        # rijksapps.nl is the cluster infra domain, not a selectable app base domain
        assert "rijksapps.nl" not in domains

    def test_all_clusters_domains(self):
        """get_supported_base_domains returns all domains when no cluster specified."""
        from opi.connectors.subdomain import get_supported_base_domains

        domains = get_supported_base_domains()
        assert "kind" in domains
        assert "rijks.app" in domains
        assert len(domains) >= 4


class TestBaseDomainValidationInRegister:
    """Tests for base domain validation in SubdomainConnector.register."""

    @pytest.mark.asyncio
    async def test_register_validates_base_domain(self):
        """register raises BaseDomainValidationError for invalid base domain."""
        from opi.connectors.subdomain import BaseDomainValidationError

        connector = SubdomainConnector()

        # Custom domains with valid syntax are now accepted, so use a syntactically invalid domain
        with pytest.raises(BaseDomainValidationError) as exc_info:
            await connector.register(
                subdomain="myapp",
                base_domain="-invalid",
                project_name="my-project",
                deployment_name="prod",
                cluster="local",
            )

        assert "ondersteund" in str(exc_info.value).lower()


class TestSubdomainRollbackHelper:
    """Tests for subdomain rollback functionality in ProjectManager."""

    @pytest.mark.asyncio
    async def test_rollback_helper_clears_pending_rollback(self):
        """_rollback_subdomain_if_needed clears pending rollback after success."""

        # Create a mock ProjectManager-like object
        class MockProjectManager:
            _pending_subdomain_rollback = None

            async def _rollback_subdomain_if_needed(self) -> bool:
                rollback_info = getattr(self, "_pending_subdomain_rollback", None)
                if not rollback_info or not rollback_info.get("should_rollback"):
                    return False

                connector = rollback_info.get("connector")
                if not connector:
                    return False

                try:
                    await connector.delete_by_deployment(
                        rollback_info["project_name"], rollback_info["deployment_name"]
                    )
                    self._pending_subdomain_rollback = None
                    return True
                except Exception:
                    return False

        manager = MockProjectManager()
        mock_connector = AsyncMock()
        mock_connector.delete_by_deployment = AsyncMock(return_value=1)

        manager._pending_subdomain_rollback = {
            "should_rollback": True,
            "connector": mock_connector,
            "project_name": "test-project",
            "deployment_name": "main",
            "subdomain": "myapp",
            "base_domain": "test.com",
        }

        result = await manager._rollback_subdomain_if_needed()

        assert result is True
        assert manager._pending_subdomain_rollback is None
        mock_connector.delete_by_deployment.assert_called_once_with("test-project", "main")

    @pytest.mark.asyncio
    async def test_rollback_helper_returns_false_when_no_pending(self):
        """_rollback_subdomain_if_needed returns False when nothing to rollback."""

        class MockProjectManager:
            _pending_subdomain_rollback = None

            async def _rollback_subdomain_if_needed(self) -> bool:
                rollback_info = getattr(self, "_pending_subdomain_rollback", None)
                return not (not rollback_info or not rollback_info.get("should_rollback"))

        manager = MockProjectManager()
        result = await manager._rollback_subdomain_if_needed()

        assert result is False

    @pytest.mark.asyncio
    async def test_rollback_helper_returns_false_when_should_rollback_false(self):
        """_rollback_subdomain_if_needed respects should_rollback flag."""

        class MockProjectManager:
            _pending_subdomain_rollback: ClassVar[dict[str, bool]] = {"should_rollback": False}

            async def _rollback_subdomain_if_needed(self) -> bool:
                rollback_info = getattr(self, "_pending_subdomain_rollback", None)
                return not (not rollback_info or not rollback_info.get("should_rollback"))

        manager = MockProjectManager()
        result = await manager._rollback_subdomain_if_needed()

        assert result is False


class TestBareDomainConstant:
    """Tests for the BARE_DOMAIN_SUBDOMAIN constant."""

    def test_bare_domain_subdomain_is_at_sign(self):
        """The bare domain sentinel is '@' (DNS convention for apex)."""
        assert BARE_DOMAIN_SUBDOMAIN == "@"


class TestFindDeploymentsForDomainItem:
    """find_deployments_for_domain_item maps an approval item to deployment names."""

    PROJECT: ClassVar[dict] = {
        "deployments": [
            {"name": "prod", "base-domain": "voorbeeld.nl", "subdomain": "app"},
            {"name": "staging", "base-domain": "voorbeeld.nl", "subdomain": "test"},
            {"name": "shared", "base-domain": "voorbeeld.nl", "subdomain": "app"},
            {"name": "other", "base-domain": "andere.nl", "subdomain": "app"},
            {"name": "default", "base-domain": "rijks.app", "subdomain": "x"},
        ]
    }

    def test_domain_item_matches_all_on_base_domain(self):
        """A domain-level item matches every deployment on that base domain, any subdomain."""
        item = {"type": "domain", "domain": "voorbeeld.nl"}
        assert find_deployments_for_domain_item(self.PROJECT, item) == ["prod", "staging", "shared"]

    def test_subdomain_item_matches_base_domain_and_subdomain(self):
        """A subdomain item additionally requires the subdomain to match."""
        item = {"type": "subdomain", "domain": "voorbeeld.nl", "name": "app"}
        assert find_deployments_for_domain_item(self.PROJECT, item) == ["prod", "shared"]

    def test_no_match_returns_empty(self):
        """Domain that no deployment uses yields an empty list (deploy nothing)."""
        item = {"type": "domain", "domain": "ongebruikt.nl"}
        assert find_deployments_for_domain_item(self.PROJECT, item) == []

    def test_subdomain_no_match_returns_empty(self):
        item = {"type": "subdomain", "domain": "voorbeeld.nl", "name": "nope"}
        assert find_deployments_for_domain_item(self.PROJECT, item) == []

    def test_missing_domain_returns_empty(self):
        assert find_deployments_for_domain_item(self.PROJECT, {"type": "domain"}) == []

    def test_handles_non_dict_and_unnamed_deployments(self):
        project = {
            "deployments": ["junk", {"base-domain": "voorbeeld.nl"}, {"name": "ok", "base-domain": "voorbeeld.nl"}]
        }
        item = {"type": "domain", "domain": "voorbeeld.nl"}
        assert find_deployments_for_domain_item(project, item) == ["ok"]


# ---------------------------------------------------------------------------
# Real-Postgres connector tests (RC-5 persistence phase 2)
#
# These replace the former asyncpg-mock tests. The connector now runs ORM queries on a
# real throwaway Postgres (the `orm_db` fixture), so ON CONFLICT uniqueness, transactions
# and CRUD are exercised for real instead of asserting mock call args.
# ---------------------------------------------------------------------------

_BASE = "rijks.app"
_CLUSTER = "odcn-production"


async def _register(conn, sub, *, project="p1", deployment="d1", base=_BASE, cluster=_CLUSTER, created_by=None):
    return await conn.register(sub, base, project, deployment, cluster, created_by)


class TestConnectorAvailabilityAndRegister:
    async def test_check_availability_reflects_registration(self, orm_db):
        c = SubdomainConnector()
        assert await c.check_availability("app", _BASE) is True
        await _register(c, "app")
        assert await c.check_availability("app", _BASE) is False

    async def test_register_persists_and_returns_row(self, orm_db):
        c = SubdomainConnector()
        row = await _register(c, "app", project="proj-a", deployment="prod", created_by="u@example.nl")
        assert row["subdomain"] == "app"
        assert row["base_domain"] == _BASE
        assert row["project_name"] == "proj-a"
        assert row["deployment_name"] == "prod"
        assert row["created_by"] == "u@example.nl"
        assert row["id"] is not None
        assert row["created_at"] is not None  # server default filled

    async def test_register_duplicate_raises_unavailable(self, orm_db):
        # A taken subdomain is rejected at layer 1 (check_availability), whichever project
        # re-registers it. (The same-project "return existing" path is layer-2 race
        # idempotency for a concurrent insert, not reachable sequentially.)
        c = SubdomainConnector()
        await _register(c, "app", project="proj-a")
        with pytest.raises(SubdomainNotAvailableError):
            await _register(c, "app", project="proj-a")
        with pytest.raises(SubdomainNotAvailableError):
            await _register(c, "app", project="proj-b")

    async def test_register_invalid_subdomain_raises(self, orm_db):
        with pytest.raises(SubdomainValidationError):
            await _register(SubdomainConnector(), "Not_Valid")


class TestConnectorReads:
    async def test_get_by_subdomain_deployment_and_project(self, orm_db):
        c = SubdomainConnector()
        await _register(c, "a", project="p1", deployment="d1")
        await _register(c, "b", project="p1", deployment="d2")
        assert (await c.get_by_subdomain("a", _BASE))["deployment_name"] == "d1"
        assert await c.get_by_subdomain("missing", _BASE) is None
        assert (await c.get_by_deployment("p1", "d2"))["subdomain"] == "b"
        assert await c.get_by_deployment("p1", "nope") is None
        rows = await c.get_by_project("p1")
        assert [r["subdomain"] for r in rows] == ["a", "b"]  # ordered

    async def test_count_and_list_all_ordered(self, orm_db):
        c = SubdomainConnector()
        await _register(c, "b")
        await _register(c, "a", deployment="d2")
        assert await c.count_all() == 2
        assert [r["subdomain"] for r in await c.list_all()] == ["a", "b"]


class TestConnectorDeletes:
    async def test_delete_single(self, orm_db):
        c = SubdomainConnector()
        await _register(c, "app")
        assert await c.delete("app", _BASE) is True
        assert await c.delete("app", _BASE) is False  # already gone
        assert await c.check_availability("app", _BASE) is True

    async def test_delete_by_deployment_and_project(self, orm_db):
        c = SubdomainConnector()
        await _register(c, "a", project="p1", deployment="d1")
        await _register(c, "b", project="p1", deployment="d2")
        await _register(c, "c", project="p2", deployment="d1")
        assert await c.delete_by_deployment("p1", "d1") == 1  # only p1/a
        assert await c.delete_by_project("p1") == 1  # only p1/b remains
        assert await c.count_all() == 1  # p2/c


class TestConnectorUpdate:
    async def test_update_changes_fields(self, orm_db):
        c = SubdomainConnector()
        await _register(c, "app", deployment="d1", cluster=_CLUSTER)
        updated = await c.update("app", _BASE, deployment_name="d2")
        assert updated["deployment_name"] == "d2"
        assert (await c.get_by_subdomain("app", _BASE))["deployment_name"] == "d2"

    async def test_update_missing_returns_none(self, orm_db):
        assert await SubdomainConnector().update("nope", _BASE, deployment_name="x") is None


class TestConnectorBareDomain:
    async def test_register_and_delete_bare_domain(self, orm_db):
        c = SubdomainConnector()
        row = await c.register_bare_domain("voorbeeld.nl", "p1", "d1", _CLUSTER)
        assert row["subdomain"] == BARE_DOMAIN_SUBDOMAIN
        assert row["base_domain"] == "voorbeeld.nl"
        assert await c.check_availability(BARE_DOMAIN_SUBDOMAIN, "voorbeeld.nl") is False
        assert await c.delete_bare_domain("voorbeeld.nl") is True
        assert await c.check_availability(BARE_DOMAIN_SUBDOMAIN, "voorbeeld.nl") is True

    async def test_delete_is_scoped_to_the_owning_project(self, orm_db):
        """The cleanup path passes its own project name: naming another tenant's domain in
        a project file must not wipe THEIR apex registration."""
        c = SubdomainConnector()
        await c.register_bare_domain("voorbeeld.nl", "p1", "d1", _CLUSTER)
        assert await c.delete_bare_domain("voorbeeld.nl", project_name="p2") is False
        assert await c.check_availability(BARE_DOMAIN_SUBDOMAIN, "voorbeeld.nl") is False
        assert await c.delete_bare_domain("voorbeeld.nl", project_name="p1") is True
        assert await c.check_availability(BARE_DOMAIN_SUBDOMAIN, "voorbeeld.nl") is True


class TestConnectorRegisterOrUpdateAtomic:
    async def test_unchanged_returns_existing(self, orm_db):
        c = SubdomainConnector()
        first = await c.register_or_update_for_deployment("app", _BASE, "p1", "d1", _CLUSTER)
        same = await c.register_or_update_for_deployment("app", _BASE, "p1", "d1", _CLUSTER)
        assert same["id"] == first["id"]

    async def test_changed_moves_subdomain(self, orm_db):
        c = SubdomainConnector()
        await c.register_or_update_for_deployment("old", _BASE, "p1", "d1", _CLUSTER)
        await c.register_or_update_for_deployment("new", _BASE, "p1", "d1", _CLUSTER)
        assert await c.check_availability("old", _BASE) is True  # old released
        assert (await c.get_by_deployment("p1", "d1"))["subdomain"] == "new"
        assert await c.count_all() == 1

    async def test_change_to_taken_preserves_old(self, orm_db):
        c = SubdomainConnector()
        await c.register_or_update_for_deployment("mine", _BASE, "p1", "d1", _CLUSTER)
        await _register(c, "taken", project="p2", deployment="d9")  # taken by another
        with pytest.raises(SubdomainNotAvailableError):
            await c.register_or_update_for_deployment("taken", _BASE, "p1", "d1", _CLUSTER)
        # the atomic change rolled back -> the old subdomain is preserved
        assert (await c.get_by_deployment("p1", "d1"))["subdomain"] == "mine"


class TestConnectorAuditLogging:
    async def test_register_and_delete_emit_audit(self, orm_db):
        with patch("opi.services.persistence.subdomain_registry.audit_logger") as audit:
            c = SubdomainConnector()
            await _register(c, "app")
            await c.delete("app", _BASE)
        messages = [call.args[0] for call in audit.info.call_args_list]
        assert any("SUBDOMAIN_REGISTERED" in m for m in messages)
        assert any("SUBDOMAIN_DELETED" in m for m in messages)
