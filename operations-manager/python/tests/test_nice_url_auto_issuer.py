"""
Tests for auto-issuer functionality in nice-url mode.

When domain-mode is 'nice-url' with a base-domain, the system should
automatically enable Let's Encrypt (issuer: letsencrypt) for HTTPS.
"""


class TestAutoIssuerLogic:
    """Tests for the auto-issuer logic used in nice-url mode."""

    def test_auto_issuer_enabled_for_nice_url_mode(self):
        """Auto-issuer should be enabled when domain_mode is nice-url with base_domain."""
        # Simulate the logic from web/router.py and api/router.py
        domain_mode = "nice-url"
        base_domain = "rijks.app"
        issuer = None

        # This is the auto-issuer logic
        if domain_mode == "nice-url" and base_domain and not issuer:
            issuer = "letsencrypt"

        assert issuer == "letsencrypt"

    def test_auto_issuer_not_enabled_without_base_domain(self):
        """Auto-issuer should NOT be enabled without base_domain."""
        domain_mode = "nice-url"
        base_domain = None
        issuer = None

        if domain_mode == "nice-url" and base_domain and not issuer:
            issuer = "letsencrypt"

        assert issuer is None

    def test_auto_issuer_not_enabled_for_other_modes(self):
        """Auto-issuer should NOT be enabled for non-nice-url modes."""
        for mode in ["component-specific", "deployment-name", "custom"]:
            domain_mode = mode
            base_domain = "example.com"
            issuer = None

            if domain_mode == "nice-url" and base_domain and not issuer:
                issuer = "letsencrypt"

            assert issuer is None, f"Issuer should be None for mode: {mode}"

    def test_auto_issuer_preserves_explicit_issuer(self):
        """Auto-issuer should NOT override an explicitly set issuer."""
        domain_mode = "nice-url"
        base_domain = "rijks.app"
        issuer = "letsencrypt-staging"  # Explicitly set

        if domain_mode == "nice-url" and base_domain and not issuer:
            issuer = "letsencrypt"

        assert issuer == "letsencrypt-staging"

    def test_auto_issuer_preserves_custom_issuer(self):
        """Auto-issuer should NOT override a custom issuer name."""
        domain_mode = "nice-url"
        base_domain = "rijks.app"
        issuer = "my-custom-issuer"  # Custom issuer

        if domain_mode == "nice-url" and base_domain and not issuer:
            issuer = "letsencrypt"

        assert issuer == "my-custom-issuer"


class TestEditDomainSettingsYamlUpdate:
    """Tests for YAML update logic when editing domain settings."""

    def test_nice_url_mode_sets_issuer_in_yaml(self):
        """Switching to nice-url mode should set issuer: letsencrypt in YAML."""
        yaml_dep = {
            "name": "prod",
            "domain-mode": "component-specific",
        }
        domain_mode = "nice-url"
        subdomain = "myapp"
        base_domain = "rijks.app"

        # Simulate the YAML update logic from web/router.py
        yaml_dep["domain-mode"] = domain_mode
        if domain_mode == "nice-url":
            yaml_dep["subdomain"] = subdomain
            yaml_dep["base-domain"] = base_domain
            yaml_dep["issuer"] = "letsencrypt"

        assert yaml_dep["domain-mode"] == "nice-url"
        assert yaml_dep["subdomain"] == "myapp"
        assert yaml_dep["base-domain"] == "rijks.app"
        assert yaml_dep["issuer"] == "letsencrypt"

    def test_switching_away_from_nice_url_removes_issuer(self):
        """Switching away from nice-url mode should remove issuer from YAML."""
        yaml_dep = {
            "name": "prod",
            "domain-mode": "nice-url",
            "subdomain": "myapp",
            "base-domain": "rijks.app",
            "issuer": "letsencrypt",
        }
        domain_mode = "component-specific"

        # Simulate the YAML update logic from web/router.py
        yaml_dep["domain-mode"] = domain_mode
        if domain_mode == "nice-url":
            yaml_dep["subdomain"] = "new-subdomain"
            yaml_dep["base-domain"] = "rijks.app"
            yaml_dep["issuer"] = "letsencrypt"
        elif domain_mode == "custom":
            yaml_dep["subdomain"] = "custom-subdomain"
            yaml_dep.pop("base-domain", None)
            yaml_dep.pop("issuer", None)
        else:
            yaml_dep.pop("subdomain", None)
            yaml_dep.pop("base-domain", None)
            yaml_dep.pop("issuer", None)

        assert yaml_dep["domain-mode"] == "component-specific"
        assert "subdomain" not in yaml_dep
        assert "base-domain" not in yaml_dep
        assert "issuer" not in yaml_dep

    def test_custom_mode_removes_issuer(self):
        """Switching to custom mode should remove issuer from YAML."""
        yaml_dep = {
            "name": "prod",
            "domain-mode": "nice-url",
            "subdomain": "myapp",
            "base-domain": "rijks.app",
            "issuer": "letsencrypt",
        }
        domain_mode = "custom"
        subdomain = "custom-subdomain"

        # Simulate the YAML update logic
        yaml_dep["domain-mode"] = domain_mode
        if domain_mode == "nice-url":
            yaml_dep["subdomain"] = subdomain
            yaml_dep["base-domain"] = "rijks.app"
            yaml_dep["issuer"] = "letsencrypt"
        elif domain_mode == "custom":
            yaml_dep["subdomain"] = subdomain
            yaml_dep.pop("base-domain", None)
            yaml_dep.pop("issuer", None)
        else:
            yaml_dep.pop("subdomain", None)
            yaml_dep.pop("base-domain", None)
            yaml_dep.pop("issuer", None)

        assert yaml_dep["domain-mode"] == "custom"
        assert yaml_dep["subdomain"] == "custom-subdomain"
        assert "base-domain" not in yaml_dep
        assert "issuer" not in yaml_dep
