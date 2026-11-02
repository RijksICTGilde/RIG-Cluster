"""In-memory stand-in voor ``SessionStore`` (RC-390), gedeeld door unit- en e2e-tests.

Zelfde reden als de in-memory diensten in ``tests/e2e/testserver.py``: geen Postgres in
die harnassen, en de middleware moet tegen iets aan. Houdt naast de rijen ook bij hoe
vaak er gelezen is, zodat een toets kan aantonen dat een statisch pad de store niet raakt.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


class InMemorySessionStore:
    """Zelfde interface als ``opi.services.session_store.SessionStore``, in het geheugen."""

    def __init__(self) -> None:
        self._rows: dict[str, dict[str, Any]] = {}
        self.reads = 0
        self.writes = 0

    def seed(self, session_id: str, data: dict[str, Any]) -> str:
        """Synchroon een sessie neerzetten, voor fixtures buiten een event loop om."""
        self._rows[session_id] = deepcopy(data)
        return session_id

    async def get(self, session_id: str) -> dict[str, Any] | None:
        self.reads += 1
        row = self._rows.get(session_id)
        return deepcopy(row) if row is not None else None

    async def set(self, session_id: str, data: dict[str, Any]) -> None:
        self.writes += 1
        self._rows[session_id] = deepcopy(data)

    async def delete(self, session_id: str) -> None:
        self._rows.pop(session_id, None)

    async def delete_by_email(self, email: str) -> int:
        normalized = email.strip().lower()
        matches = [
            sid
            for sid, data in self._rows.items()
            if str((data.get("user") or {}).get("email") or "").strip().lower() == normalized
        ]
        for sid in matches:
            del self._rows[sid]
        return len(matches)

    async def touch(self, session_id: str) -> bool:
        return session_id in self._rows

    async def count(self) -> int:
        return len(self._rows)

    async def purge_expired(self) -> int:
        return 0
