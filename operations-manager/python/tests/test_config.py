"""
Tests for opi.core.config module.

Tests env file discovery, env file parsing/warnings, and SOPS key parsing.
"""

import logging
from unittest.mock import mock_open, patch

import pytest
from opi.core.config import (
    PROEFBAAR_CLUSTER,
    Settings,
    _check_env_file_for_environment_var,
    _get_env_files,
    _load_project_kek,
    parse_sops_age_key_content,
)


class TestCheckEnvFileForEnvironmentVar:
    """Tests for _check_env_file_for_environment_var."""

    def test_warns_on_environment_var(self, caplog):
        """Should log warnings when ENVIRONMENT= is found in an env file."""
        file_content = "DEBUG=true\nENVIRONMENT=production\nOTHER=val\n"
        with (
            patch("builtins.open", mock_open(read_data=file_content)),
            caplog.at_level(logging.WARNING),
        ):
            _check_env_file_for_environment_var("/fake/.env")

        assert any("ENVIRONMENT variable found" in r.message for r in caplog.records)
        assert any("IGNORED" in r.message for r in caplog.records)

    def test_no_warning_without_environment_var(self, caplog):
        """Should not warn when ENVIRONMENT is absent."""
        file_content = "DEBUG=true\nSECRET_KEY=abc\n"
        with (
            patch("builtins.open", mock_open(read_data=file_content)),
            caplog.at_level(logging.WARNING),
        ):
            _check_env_file_for_environment_var("/fake/.env")

        assert not any("ENVIRONMENT variable found" in r.message for r in caplog.records)

    def test_skips_comments_and_empty_lines(self, caplog):
        """Should skip comments and blank lines."""
        file_content = "# ENVIRONMENT=production\n\n  \nDEBUG=true\n"
        with (
            patch("builtins.open", mock_open(read_data=file_content)),
            caplog.at_level(logging.WARNING),
        ):
            _check_env_file_for_environment_var("/fake/.env")

        assert not any("ENVIRONMENT variable found" in r.message for r in caplog.records)

    def test_handles_environment_with_space(self, caplog):
        """Should detect ENVIRONMENT with space separator."""
        file_content = "ENVIRONMENT local\n"
        with (
            patch("builtins.open", mock_open(read_data=file_content)),
            caplog.at_level(logging.WARNING),
        ):
            _check_env_file_for_environment_var("/fake/.env")

        assert any("ENVIRONMENT variable found" in r.message for r in caplog.records)

    def test_handles_file_read_error(self, caplog):
        """Should handle file read errors gracefully."""
        with (
            patch("builtins.open", side_effect=PermissionError("denied")),
            caplog.at_level(logging.DEBUG),
        ):
            _check_env_file_for_environment_var("/fake/.env")

        assert any("Could not check" in r.message for r in caplog.records)


class TestGetEnvFiles:
    """Tests for _get_env_files."""

    def setup_method(self):
        """Reset the cache before each test."""
        import opi.core.config as config_module

        config_module._env_files_cache = None

    def teardown_method(self):
        """Reset the cache after each test."""
        import opi.core.config as config_module

        config_module._env_files_cache = None

    @patch("opi.core.config._check_env_file_for_environment_var")
    @patch("os.path.exists")
    @patch.dict("os.environ", {"ENVIRONMENT": "local"}, clear=False)
    def test_loads_base_and_local_env(self, mock_exists, mock_check):
        """Should find .env and .env.local when both exist."""
        mock_exists.side_effect = lambda p: p in (".env", ".env.local")

        result = _get_env_files()

        assert ".env" in result
        assert ".env.local" in result

    @patch("opi.core.config._check_env_file_for_environment_var")
    @patch("os.path.exists")
    @patch.dict("os.environ", {"ENVIRONMENT": "production,kubernetes"}, clear=False)
    def test_loads_comma_separated_environments(self, mock_exists, mock_check):
        """Should handle comma-separated ENVIRONMENT values."""
        mock_exists.side_effect = lambda p: p in (".env", ".env.production", ".env.kubernetes")

        result = _get_env_files()

        assert ".env" in result
        assert ".env.production" in result
        assert ".env.kubernetes" in result

    @patch("opi.core.config._check_env_file_for_environment_var")
    @patch("os.path.exists")
    @patch.dict("os.environ", {"ENVIRONMENT": "staging"}, clear=False)
    def test_missing_env_specific_file(self, mock_exists, mock_check, caplog):
        """A missing environment-specific file is optional: logged at DEBUG, never ERROR."""
        mock_exists.side_effect = lambda p: p == ".env"

        with caplog.at_level(logging.DEBUG):
            result = _get_env_files()

        assert ".env" in result
        assert ".env.staging" not in result
        assert any("No environment-specific file .env.staging" in r.message for r in caplog.records)
        assert not any(r.levelno >= logging.ERROR for r in caplog.records)

    @patch("opi.core.config._check_env_file_for_environment_var")
    @patch("os.path.exists")
    @patch.dict("os.environ", {"ENVIRONMENT": "local"}, clear=False)
    def test_caches_result(self, mock_exists, mock_check):
        """Should return cached result on second call."""
        mock_exists.side_effect = lambda p: p in (".env", ".env.local")

        result1 = _get_env_files()
        result2 = _get_env_files()

        assert result1 is result2

    @patch("opi.core.config._check_env_file_for_environment_var")
    @patch("os.path.exists")
    @patch.dict("os.environ", {"ENVIRONMENT": "local", "CONFIG_ENV_FILE_PATH": "/custom/.env"}, clear=False)
    def test_configmap_custom_path(self, mock_exists, mock_check):
        """Should check custom CONFIG_ENV_FILE_PATH."""
        mock_exists.side_effect = lambda p: p in (".env", ".env.local", "/custom/.env")

        result = _get_env_files()

        assert "/custom/.env" in result

    @patch("opi.core.config._check_env_file_for_environment_var")
    @patch("os.path.exists")
    @patch.dict("os.environ", {}, clear=True)
    def test_zonder_environment_laadt_env_production(self, mock_exists, mock_check):
        """conftest.py zet ENVIRONMENT altijd op 'local' voor de testrun, dus hier expliciet
        leeg: zonder de variabele moet dit dezelfde 'production'-default lezen als het
        Settings-veld, anders zeggen de twee lezers iets anders (RC-321).
        """
        mock_exists.side_effect = lambda p: p in (".env", ".env.production")

        result = _get_env_files()

        assert ".env.production" in result
        assert ".env.local" not in result


class TestParseSopsAgeKeyContent:
    """Tests for parse_sops_age_key_content."""

    def test_parses_valid_key_content(self):
        """Should extract public and private keys from valid content."""
        content = "# created: 2024-01-01\n# public key: age1abc123\nAGE-SECRET-KEY-1ABCDEF\n"
        public, private = parse_sops_age_key_content(content)
        assert public == "age1abc123"
        assert private == "AGE-SECRET-KEY-1ABCDEF"

    def test_returns_none_for_empty_content(self):
        """Should return (None, None) for empty input."""
        assert parse_sops_age_key_content("") == (None, None)

    def test_returns_none_for_none_like_empty(self):
        """Should return (None, None) for falsy input."""
        assert parse_sops_age_key_content("") == (None, None)

    def test_handles_content_with_only_public_key(self):
        """Should extract public key even if private key is missing."""
        content = "# public key: age1abc123\n"
        public, private = parse_sops_age_key_content(content)
        assert public == "age1abc123"
        assert private is None

    def test_handles_content_with_only_private_key(self):
        """Should extract private key even if public key comment is missing."""
        content = "AGE-SECRET-KEY-1ABCDEF\n"
        public, private = parse_sops_age_key_content(content)
        assert public is None
        assert private == "AGE-SECRET-KEY-1ABCDEF"

    def test_handles_extra_whitespace(self):
        """Should handle lines with extra whitespace."""
        content = "  # public key:  age1abc123  \n  AGE-SECRET-KEY-1ABCDEF  \n"
        public, private = parse_sops_age_key_content(content)
        assert public == "age1abc123"
        assert private == "AGE-SECRET-KEY-1ABCDEF"


class TestLoadProjectKek:
    """Tests for _load_project_kek: the project-KEK, zonder KEK verandert er niets (RC-334)."""

    KEK_CONTENT = "# created: today\n# public key: age1kek00000000000000000000000000000000000000000000000kek\nAGE-SECRET-KEY-1KEK\n"

    def test_without_a_file_setting_nothing_changes(self):
        settings = Settings()
        settings.PROJECT_KEK_FILE = None

        _load_project_kek(settings)

        assert settings.PROJECT_KEK_PUBLIC_KEY is None
        assert settings.PROJECT_KEK_PRIVATE_KEY is None

    def test_a_mounted_kek_file_sets_both_halves(self, tmp_path):
        key_file = tmp_path / "project-kek.txt"
        key_file.write_text(self.KEK_CONTENT)
        settings = Settings()
        settings.PROJECT_KEK_FILE = str(key_file)

        _load_project_kek(settings)

        assert settings.PROJECT_KEK_PUBLIC_KEY == "age1kek00000000000000000000000000000000000000000000000kek"
        assert settings.PROJECT_KEK_PRIVATE_KEY == "AGE-SECRET-KEY-1KEK"

    def test_a_missing_mount_falls_back_to_no_kek_with_a_warning(self, tmp_path, caplog):
        """Het secret is optional: een niet-gemount pad is de bedoelde uitkomst, geen opstartfout."""
        settings = Settings()
        settings.PROJECT_KEK_FILE = str(tmp_path / "not-mounted.txt")

        with caplog.at_level(logging.WARNING):
            _load_project_kek(settings)

        assert settings.PROJECT_KEK_PUBLIC_KEY is None
        assert settings.PROJECT_KEK_PRIVATE_KEY is None
        assert any("niet gemount" in r.message for r in caplog.records)

    def test_an_empty_kek_file_is_a_hard_startup_error(self, tmp_path):
        """Een gezet maar leeg bestand mag niet stil terugvallen: dat verbergt een verkeerde mount."""
        key_file = tmp_path / "project-kek.txt"
        key_file.write_text("")
        settings = Settings()
        settings.PROJECT_KEK_FILE = str(key_file)

        with pytest.raises(RuntimeError, match="Project-KEK is leeg"):
            _load_project_kek(settings)

    def test_a_kek_file_without_a_private_key_is_refused(self, tmp_path):
        key_file = tmp_path / "project-kek.txt"
        key_file.write_text("# public key: age1kek00000000000000000000000000000000000000000000000kek\n")
        settings = Settings()
        settings.PROJECT_KEK_FILE = str(key_file)

        with pytest.raises(RuntimeError, match="bevat geen AGE-sleutelpaar"):
            _load_project_kek(settings)

    def test_existing_values_are_not_overwritten(self, tmp_path):
        """De env-terugval (``PROJECT_KEK_PRIVATE_KEY`` al gezet) blijft staan boven het bestand."""
        key_file = tmp_path / "project-kek.txt"
        key_file.write_text(self.KEK_CONTENT)
        settings = Settings()
        settings.PROJECT_KEK_FILE = str(key_file)
        settings.PROJECT_KEK_PUBLIC_KEY = "age1already-set"
        settings.PROJECT_KEK_PRIVATE_KEY = "AGE-SECRET-KEY-1ALREADY-SET"

        _load_project_kek(settings)

        assert settings.PROJECT_KEK_PUBLIC_KEY == "age1already-set"
        assert settings.PROJECT_KEK_PRIVATE_KEY == "AGE-SECRET-KEY-1ALREADY-SET"


class TestTaskCleanupRetentionWindows:
    """RC-282: de kop blijft 90 dagen, de werkgegevens volgens het korte venster."""

    def test_defaults_without_environment(self):
        settings = Settings()

        assert settings.TASK_WORKER_CLEANUP_RETENTION_HOURS == 1
        assert settings.TASK_WORKER_HEADER_RETENTION_DAYS == 90


class TestProeftokenAlleenOpDeSandbox:
    """SECURITY_PROOF_TOKEN maakt een instantie een doel van de aanvalsproef."""

    def test_de_sandbox_mag_een_proeftoken_dragen(self):
        settings = Settings(CLUSTER_MANAGER=PROEFBAAR_CLUSTER, SECURITY_PROOF_TOKEN="een-token-van-deze-run")
        assert settings.SECURITY_PROOF_TOKEN == "een-token-van-deze-run"

    @pytest.mark.parametrize("cluster", ["odcn-production", "odcn-staging", "local"])
    def test_elk_ander_cluster_weigert_te_starten(self, cluster):
        """Faalconditie: een gezet token op staging of productie laat OPI gewoon starten."""
        with pytest.raises(RuntimeError, match="SECURITY_PROOF_TOKEN"):
            Settings(CLUSTER_MANAGER=cluster, SECURITY_PROOF_TOKEN="een-token-van-deze-run")

    @pytest.mark.parametrize("cluster", ["odcn-production", "sandboxed-local"])
    def test_zonder_token_verandert_er_niets(self, cluster):
        assert Settings(CLUSTER_MANAGER=cluster).SECURITY_PROOF_TOKEN == ""
