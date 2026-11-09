"""The outcome of one deployment's round, written by the manifest generation."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DeploymentResult:
    """Result information for a processed deployment."""

    deployment_name: str
    cluster: str
    namespace: str
    urls: dict[str, str] = field(default_factory=dict)  # component_name -> public_url
    status: str = "success"
    errors: list[str] = field(default_factory=list)
