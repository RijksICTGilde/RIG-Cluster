"""Tests for opi.utils.project_utils module."""

import logging

from opi.utils.project_utils import normalize_container_image


class TestNormalizeContainerImage:
    """OCI spec requires lowercase repository names — normalization must catch violations."""

    def test_already_lowercase_is_noop(self):
        image, was_normalized = normalize_container_image("ghcr.io/org/repo:latest")
        assert image == "ghcr.io/org/repo:latest"
        assert was_normalized is False

    def test_mixed_case_is_lowercased(self):
        image, was_normalized = normalize_container_image(
            "ghcr.io/BureauArchitectuurDigitaleOverheid/bouwmeester/backend:main"
        )
        assert image == "ghcr.io/bureauarchitectuurdigitaleoverheid/bouwmeester/backend:main"
        assert was_normalized is True

    def test_uppercase_tag_is_also_lowercased(self):
        """Tags are part of the image reference and get lowercased too."""
        image, was_normalized = normalize_container_image("nginx:Latest")
        assert image == "nginx:latest"
        assert was_normalized is True

    def test_fully_uppercase(self):
        image, was_normalized = normalize_container_image("GHCR.IO/ORG/REPO:V1")
        assert image == "ghcr.io/org/repo:v1"
        assert was_normalized is True

    def test_digest_reference(self):
        ref = "ghcr.io/Org/Repo@sha256:abc123"
        image, was_normalized = normalize_container_image(ref)
        assert image == "ghcr.io/org/repo@sha256:abc123"
        assert was_normalized is True

    def test_simple_image_no_registry(self):
        image, was_normalized = normalize_container_image("nginx:1.21")
        assert image == "nginx:1.21"
        assert was_normalized is False

    def test_logs_warning_on_normalization(self, caplog):
        with caplog.at_level(logging.WARNING, logger="opi.utils.project_utils"):
            normalize_container_image("ghcr.io/MyOrg/MyRepo:latest")
        assert "uppercase" in caplog.text
        assert "ghcr.io/MyOrg/MyRepo:latest" in caplog.text
        assert "ghcr.io/myorg/myrepo:latest" in caplog.text

    def test_no_warning_when_already_lowercase(self, caplog):
        with caplog.at_level(logging.WARNING, logger="opi.utils.project_utils"):
            normalize_container_image("ghcr.io/myorg/myrepo:latest")
        assert caplog.text == ""
