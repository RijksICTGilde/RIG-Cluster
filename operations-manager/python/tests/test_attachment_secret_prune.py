"""A removed file-mode attachment takes its Secret manifest with it.

Found by ``test_delete_attachment_removes_it_everywhere`` in tasks_live: the Secret is named
``<deployment>-attch-<reference>``, which carries neither a component prefix nor the
``<deployment>-<service>-`` prefix the two prunes key on. Its file stayed in the
deployments repo, so ArgoCD kept the attachment's content in the namespace.
"""

import os
from unittest.mock import patch

from opi.manager.project_manager import ProjectManager


def _touch(directory: str, name: str) -> None:
    with open(os.path.join(directory, name), "w") as f:
        f.write("kind: Secret\n")


def _prune(directory: str, generated: list[str]) -> list[str]:
    with patch("opi.manager.project_manager.KubectlConnector"):
        pm = ProjectManager()
    pm._prune_obsolete_service_manifests({"name": "main"}, directory, generated)
    return sorted(os.listdir(directory))


def test_the_secret_of_a_removed_attachment_is_pruned(tmp_path) -> None:
    _touch(str(tmp_path), "main-attch-bijlage-attachment-secret.sops.yaml")

    assert _prune(str(tmp_path), []) == []


def test_the_secret_of_a_current_attachment_stays(tmp_path) -> None:
    _touch(str(tmp_path), "main-attch-bijlage-attachment-secret.sops.yaml")

    remaining = _prune(str(tmp_path), ["main-attch-bijlage-attachment-secret.to-sops.yaml"])

    assert remaining == ["main-attch-bijlage-attachment-secret.sops.yaml"]


def test_another_deployment_is_left_alone(tmp_path) -> None:
    _touch(str(tmp_path), "tweede-attch-bijlage-attachment-secret.sops.yaml")

    assert _prune(str(tmp_path), []) == ["tweede-attch-bijlage-attachment-secret.sops.yaml"]
