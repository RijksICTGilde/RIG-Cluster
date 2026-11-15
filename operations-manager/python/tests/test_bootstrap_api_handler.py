"""Tests for BootstrapApiHandler._load_yaml.

Had no test at all before this file: BootstrapManager is mocked away in every test that
touches it, so the YAML-loading migration to ``load_yaml_safe`` ran untested.
"""

from pathlib import Path

import pytest
from opi.manager.bootstrap.bootstrap_api_handler import BootstrapApiHandler


class TestLoadYaml:
    def test_it_parses_a_real_bootstrap_config(self) -> None:
        handler = BootstrapApiHandler()
        config = handler._load_yaml(
            Path(__file__).resolve().parents[1] / "opi" / "configs" / "bootstrap" / "algoritmeregister-init.yaml"
        )
        assert config["variables"]["org_code"] == "RIG"
        assert config["bootstrap"][0]["name"] == "algoritmeregister-organization-setup"

    def test_a_missing_file_raises(self, tmp_path: Path) -> None:
        handler = BootstrapApiHandler()
        with pytest.raises(FileNotFoundError):
            handler._load_yaml(tmp_path / "missing.yaml")

    def test_a_non_mapping_document_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "list.yaml"
        path.write_text("- one\n- two\n", encoding="utf-8")
        handler = BootstrapApiHandler()
        with pytest.raises(TypeError, match="expected dict"):
            handler._load_yaml(path)

    def test_an_unquoted_scalar_keeps_its_yaml_1_2_reading(self, tmp_path: Path) -> None:
        """Characterizes the switch from a round-trip loader to ``load_yaml_safe``:
        both resolve an unquoted ``on`` as the string "on", not a boolean."""
        path = tmp_path / "config.yaml"
        path.write_text("variables:\n  flag: on\n", encoding="utf-8")
        handler = BootstrapApiHandler()
        assert handler._load_yaml(path)["variables"]["flag"] == "on"
