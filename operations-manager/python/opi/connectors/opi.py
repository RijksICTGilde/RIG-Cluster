"""OPI-to-OPI HTTP connector for federation routing.

Follows the HttpConnector pattern: aiohttp.ClientSession managed via
async context manager, with typed convenience methods for the OPI task API.
"""

import logging
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)


class OpiConnectorError(Exception):
    """Raised when a remote OPI instance returns an unexpected status code."""

    def __init__(self, status: int, detail: str) -> None:
        self.status = status
        self.detail = detail
        super().__init__(f"OPI peer returned {status}: {detail}")


class OpiConnector:
    """HTTP client for communicating with a remote OPI instance."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        verify_tls: bool = True,
        timeout: int = 30,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.verify_tls = verify_tls
        self.timeout = timeout
        self.session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> OpiConnector:
        connector = None if self.verify_tls else aiohttp.TCPConnector(ssl=False)
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.timeout),
            connector=connector,
        )
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self.session:
            await self.session.close()

    def _headers(self) -> dict[str, str]:
        return {
            "X-API-Key": self.api_key,
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    # API methods
    # ------------------------------------------------------------------

    async def health_check(self) -> dict:
        """GET /health on the remote OPI instance."""
        return await self._get("/health", expected_status={200})

    async def create_task(
        self,
        task_type: str,
        project_name: str,
        deployment_name: str | None,
        cluster: str,
        payload: dict,
        created_by: str | None = None,
    ) -> dict:
        """POST /api/tasks on the remote OPI instance. Expects 202."""
        body: dict[str, Any] = {
            "task_type": task_type,
            "project_name": project_name,
            "deployment_name": deployment_name,
            "cluster": cluster,
            "payload": payload,
        }
        if created_by:
            body["created_by"] = created_by
        return await self._post("/api/tasks", body=body, expected_status={202})

    async def get_task_status(self, task_id: str) -> dict:
        """GET /api/tasks/{task_id}. Accepts 200 (done) or 202 (in-progress)."""
        return await self._get(f"/api/tasks/{task_id}", expected_status={200, 202})

    async def cancel_task(self, task_id: str) -> dict:
        """POST /api/tasks/{task_id}/:cancel. Expects 200."""
        return await self._post(f"/api/tasks/{task_id}/:cancel", body=None, expected_status={200})

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get(self, path: str, expected_status: set[int]) -> dict:
        if not self.session:
            msg = "OpiConnector session not initialized. Use async context manager."
            raise RuntimeError(msg)

        url = f"{self.base_url}{path}"
        logger.info("GET %s", url)

        async with self.session.get(url, headers=self._headers()) as resp:
            data = await self._parse_response(resp)
            if resp.status not in expected_status:
                raise OpiConnectorError(resp.status, str(data))
            return data if isinstance(data, dict) else {"raw": data}

    async def _post(self, path: str, body: dict | None, expected_status: set[int]) -> dict:
        if not self.session:
            msg = "OpiConnector session not initialized. Use async context manager."
            raise RuntimeError(msg)

        url = f"{self.base_url}{path}"
        logger.info("POST %s", url)

        async with self.session.post(url, headers=self._headers(), json=body) as resp:
            data = await self._parse_response(resp)
            if resp.status not in expected_status:
                raise OpiConnectorError(resp.status, str(data))
            return data if isinstance(data, dict) else {"raw": data}

    @staticmethod
    async def _parse_response(resp: aiohttp.ClientResponse) -> dict | str:
        try:
            return await resp.json()
        except Exception:
            return await resp.text()
