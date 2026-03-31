from abc import ABC, abstractmethod
from typing import Any


class ManifestExtension(ABC):
    """Base class for manifest extensions.

    Extensions receive a parsed Kubernetes manifest (dict) and return
    a potentially modified version. Each extension type implements its
    own mutation logic.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    @abstractmethod
    def process(self, manifest: dict[str, Any]) -> dict[str, Any]:
        """Process a single Kubernetes manifest and return the (possibly modified) result."""
        ...
