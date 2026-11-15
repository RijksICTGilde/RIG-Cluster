"""Federation configuration for multi-cluster OPI routing."""

import json
import logging

from pydantic import BaseModel, field_validator

logger = logging.getLogger(__name__)


class PeerConfig(BaseModel):
    cluster: str
    url: str
    api_key: str
    verify_tls: bool = True
    timeout: int = 30

    @field_validator("url")
    @classmethod
    def strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @field_validator("cluster")
    @classmethod
    def reject_empty_cluster(cls, v: str) -> str:
        if not v or not v.strip():
            msg = "cluster must not be empty"
            raise ValueError(msg)
        return v.strip()


class FederationRegistry:
    def __init__(self, peers: list[PeerConfig], local_cluster: str) -> None:
        self._peers: dict[str, PeerConfig] = {p.cluster: p for p in peers}
        self._local_cluster = local_cluster

    @classmethod
    def from_settings(cls, federation_peers_json: str, local_cluster: str) -> FederationRegistry:
        """Raises ``ValueError`` on invalid JSON, ``TypeError`` when it is not an array.

        An empty or blank string is a registry without peers, not an error: federation is off
        by default.
        """
        if not federation_peers_json or not federation_peers_json.strip():
            return cls(peers=[], local_cluster=local_cluster)

        try:
            raw = json.loads(federation_peers_json)
        except json.JSONDecodeError as exc:
            msg = f"Invalid FEDERATION_PEERS JSON: {exc}"
            raise ValueError(msg) from exc

        if not isinstance(raw, list):
            msg = "FEDERATION_PEERS must be a JSON array"
            raise TypeError(msg)

        peers = [PeerConfig(**item) for item in raw]
        logger.info("Loaded %d federation peer(s): %s", len(peers), [p.cluster for p in peers])
        return cls(peers=peers, local_cluster=local_cluster)

    def get_peer(self, cluster: str) -> PeerConfig | None:
        return self._peers.get(cluster)

    def is_local_cluster(self, cluster: str) -> bool:
        return cluster == self._local_cluster

    def get_all_peers(self) -> list[PeerConfig]:
        return list(self._peers.values())

    def is_enabled(self) -> bool:
        return len(self._peers) > 0
