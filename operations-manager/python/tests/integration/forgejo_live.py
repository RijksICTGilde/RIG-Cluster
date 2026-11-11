"""Gedeelde hulp voor de groep ``forgejo_live``: OPI's git-laag tegen een echte Forgejo.

`task test-forgejo` start de image die het platform pint en zet het adres en de
beheerdersinlog in de omgeving. Deze module maakt met de API van die Forgejo een token en de
repositories die OPI beheert, en levert het terugleeskanaal: wat er in Forgejo staat, leest
de toets via de API en niet via de connector onder toets.

De bestaande toetsen tegen kale repo's en ``git http-backend`` blijven: die zijn snel en
meten onze eigen logica. Deze groep meet wat Forgejo daar anders aan doet.
"""

from __future__ import annotations

import base64
from typing import Any
from urllib.parse import urlparse, urlunparse
from uuid import uuid4

import httpx
from tests.integration.live import FORGEJO

TIMEOUT = 60.0

#: De drie repositories die OPI beheert, met de naam die de instellingen eraan geven.
PLATFORM_REPOS = ("zad-projects", "zad-argo-user-applications", "zad-deployments")


def base_url() -> str:
    return FORGEJO.omgeving("FORGEJO_LIVE_URL").rstrip("/")


def admin_user() -> str:
    return FORGEJO.omgeving("FORGEJO_LIVE_USER")


def admin_password() -> str:
    return FORGEJO.omgeving("FORGEJO_LIVE_PASSWORD")


def assert_local() -> None:
    FORGEJO.assert_local(base_url())


class ForgejoApi:
    """De API van Forgejo: de opstelling bouwen, en terugleren wat er echt staat."""

    def __init__(self) -> None:
        # De grendel hier en niet alleen in de fixture: elke destructieve methode hieronder
        # (fresh_repo, drop_repo) is pas bereikbaar na deze stap.
        assert_local()
        self._url = base_url()
        self._auth = (admin_user(), admin_password())
        self._token: str | None = None
        self._name = f"zad-live-{uuid4().hex[:12]}"

    @property
    def token(self) -> str:
        """Een token zoals OPI er een gebruikt, gemaakt met de beheerdersinlog."""
        if self._token is None:
            answer = httpx.post(
                f"{self._url}/api/v1/users/{admin_user()}/tokens",
                auth=self._auth,
                json={"name": self._name, "scopes": ["write:repository", "write:user"]},
                timeout=TIMEOUT,
            )
            assert answer.status_code == 201, answer.text
            self._token = answer.json()["sha1"]
        return self._token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"token {self.token}"}

    def get(self, path: str, **params: Any) -> httpx.Response:
        return httpx.get(f"{self._url}/api/v1{path}", headers=self._headers(), params=params, timeout=TIMEOUT)

    def post(self, path: str, payload: dict[str, Any]) -> httpx.Response:
        return httpx.post(f"{self._url}/api/v1{path}", headers=self._headers(), json=payload, timeout=TIMEOUT)

    def delete(self, path: str) -> httpx.Response:
        return httpx.delete(f"{self._url}/api/v1{path}", headers=self._headers(), timeout=TIMEOUT)

    def ok(self, path: str, **params: Any) -> Any:
        answer = self.get(path, **params)
        assert answer.status_code == 200, f"GET {path}: {answer.status_code} {answer.text}"
        return answer.json()

    def fresh_repo(self, name: str, *, files: dict[str, str] | None = None) -> str:
        """Een lege repository met een eerste commit, en zijn clone-URL.

        Hij gaat eerst weg: een vorige run die halverwege stopte mag de volgende niet
        besmetten.
        """
        self.delete(f"/repos/{admin_user()}/{name}")
        created = self.post("/user/repos", {"name": name, "private": True, "auto_init": True, "default_branch": "main"})
        assert created.status_code == 201, created.text
        for path, content in (files or {}).items():
            self.write_file(name, path, content, message=f"add {path}")
        return self.clone_url(name)

    def clone_url(self, name: str) -> str:
        """De HTTP-clone-URL zonder credentials, zoals een projectbestand hem draagt."""
        return f"{self._url}/{admin_user()}/{name}.git"

    def authenticated_clone_url(self, name: str) -> str:
        """Dezelfde URL met de inlog erin, voor een clone buiten OPI om."""
        parts = urlparse(self._url)
        netloc = f"{admin_user()}:{self.token}@{parts.netloc}"
        return urlunparse(parts._replace(netloc=netloc, path=f"/{admin_user()}/{name}.git"))

    def write_file(self, repo: str, path: str, content: str, *, message: str) -> str:
        """Een commit buiten OPI om, zoals een mens die met de hand maakt."""
        payload = {"content": base64.b64encode(content.encode()).decode(), "message": message, "branch": "main"}
        answer = httpx.post(
            f"{self._url}/api/v1/repos/{admin_user()}/{repo}/contents/{path}",
            headers=self._headers(),
            json=payload,
            timeout=TIMEOUT,
        )
        if answer.status_code == 422:
            existing = self.ok(f"/repos/{admin_user()}/{repo}/contents/{path}")
            answer = httpx.put(
                f"{self._url}/api/v1/repos/{admin_user()}/{repo}/contents/{path}",
                headers=self._headers(),
                json={**payload, "sha": existing["sha"]},
                timeout=TIMEOUT,
            )
        assert answer.status_code in (200, 201), answer.text
        return answer.json()["commit"]["sha"]

    def file_content(self, repo: str, path: str, ref: str = "main") -> str | None:
        answer = self.get(f"/repos/{admin_user()}/{repo}/contents/{path}", ref=ref)
        if answer.status_code == 404:
            return None
        assert answer.status_code == 200, answer.text
        return base64.b64decode(answer.json()["content"]).decode()

    def tip(self, repo: str, branch: str = "main") -> str:
        return self.ok(f"/repos/{admin_user()}/{repo}/branches/{branch}")["commit"]["id"]

    def commit_messages(self, repo: str, branch: str = "main") -> list[str]:
        return [
            entry["commit"]["message"].strip() for entry in self.ok(f"/repos/{admin_user()}/{repo}/commits", sha=branch)
        ]

    def protect_branch(self, repo: str, branch: str = "main") -> None:
        """Zet de branch dicht, zoals een beheerder dat doet; een push hoort dan te falen."""
        created = self.post(
            f"/repos/{admin_user()}/{repo}/branch_protections",
            {"rule_name": branch, "enable_push": False},
        )
        assert created.status_code in (200, 201), created.text

    def drop_repo(self, name: str) -> None:
        self.delete(f"/repos/{admin_user()}/{name}")

    def drop_token(self) -> None:
        """Haal het token weg dat deze opstelling maakte.

        Op de wegwerpcontainer kost een achtergebleven token niets, maar deze groep draait
        ook tegen de Forgejo van de sandbox, en daar stapelen ze op.
        """
        if self._token is None:
            return
        httpx.delete(f"{self._url}/api/v1/users/{admin_user()}/tokens/{self._name}", auth=self._auth, timeout=TIMEOUT)
        self._token = None
