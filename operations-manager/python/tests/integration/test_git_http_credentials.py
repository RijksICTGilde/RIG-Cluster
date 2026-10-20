"""Credentials via de omgeving, gemeten tegen een echte git-server.

De unittests in tests/test_git_credentials.py zien alleen wat de connector in de omgeving
zet, niet of git die sleutel ook gebruikt: een vorm die git stil negeert houdt ze groen
terwijl elke lees en schrijf omvalt op authenticatie. Daarom een bare repo achter
git http-backend met Basic auth op loopback. Geen cluster, alleen git.
"""

import base64
import os
import shutil
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from opi.connectors.git import GitConnector

if TYPE_CHECKING:
    from collections.abc import Iterator

GEBRUIKER = "zad"
WACHTWOORD = "zeer-geheim-token"
VERWACHTE_HEADER = "Basic " + base64.b64encode(f"{GEBRUIKER}:{WACHTWOORD}".encode()).decode()


def _git(*args: str, cwd: Path) -> str:
    """Draai git los van de connector, voor het opzetten en het nameten."""
    resultaat = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    return resultaat.stdout.strip()


def _vrije_poort() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _GitHttpServer(ThreadingHTTPServer):
    """Serveert een map met bare repo's, of stuurt alles door naar een andere basis-URL."""

    daemon_threads = True

    def __init__(self, root: Path | None, redirect_naar: str | None) -> None:
        super().__init__(("127.0.0.1", _vrije_poort()), _Handler)
        self.root = root
        self.redirect_naar = redirect_naar
        self.ontvangen_headers: list[dict[str, str]] = []

    @property
    def basis_url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _GitHttpServer

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 (naam ligt vast in BaseHTTPRequestHandler)
        """Stil: de testuitvoer is al druk genoeg."""

    def do_GET(self) -> None:
        self._behandel()

    def do_POST(self) -> None:
        self._behandel()

    def _behandel(self) -> None:
        self.server.ontvangen_headers.append(dict(self.headers))
        body = self._lees_body()

        if self.server.redirect_naar is not None:
            self._antwoord(302, [("Location", self.server.redirect_naar + self.path)], b"")
            return

        if self.headers.get("Authorization") != VERWACHTE_HEADER:
            self._antwoord(401, [("WWW-Authenticate", 'Basic realm="zad"')], b"")
            return

        self._backend(body)

    def _lees_body(self) -> bytes:
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            stukken: list[bytes] = []
            while True:
                lengte = int(self.rfile.readline().split(b";")[0], 16)
                if lengte == 0:
                    self.rfile.readline()
                    return b"".join(stukken)
                stukken.append(self.rfile.read(lengte))
                self.rfile.readline()
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def _backend(self, body: bytes) -> None:
        pad, _, query = self.path.partition("?")
        omgeving = {
            **os.environ,
            "GIT_PROJECT_ROOT": str(self.server.root),
            "GIT_HTTP_EXPORT_ALL": "1",
            "REQUEST_METHOD": self.command,
            "PATH_INFO": pad,
            "QUERY_STRING": query,
            # Zet receive-pack open, zodat de push niet op de export-regels strandt.
            "REMOTE_USER": GEBRUIKER,
            "REMOTE_ADDR": self.client_address[0],
            "CONTENT_TYPE": self.headers.get("Content-Type", ""),
            "CONTENT_LENGTH": str(len(body)),
        }
        if self.headers.get("Git-Protocol"):
            omgeving["GIT_PROTOCOL"] = self.headers["Git-Protocol"]

        resultaat = subprocess.run([_http_backend_pad()], input=body, capture_output=True, env=omgeving, timeout=60)
        koptekst, _, inhoud = resultaat.stdout.partition(b"\r\n\r\n")
        if not _:
            koptekst, _, inhoud = resultaat.stdout.partition(b"\n\n")

        status = 200
        koppen: list[tuple[str, str]] = []
        for regel in koptekst.decode().splitlines():
            naam, _, waarde = regel.partition(":")
            if naam.lower() == "status":
                status = int(waarde.strip().split()[0])
            else:
                koppen.append((naam, waarde.strip()))
        self._antwoord(status, koppen, inhoud)

    def _antwoord(self, status: int, koppen: list[tuple[str, str]], inhoud: bytes) -> None:
        self.send_response(status)
        for naam, waarde in koppen:
            self.send_header(naam, waarde)
        self.send_header("Content-Length", str(len(inhoud)))
        self.end_headers()
        self.wfile.write(inhoud)


def _http_backend_pad() -> str:
    return str(
        Path(subprocess.run(["git", "--exec-path"], capture_output=True, text=True).stdout.strip()) / "git-http-backend"
    )


def _start_server(root: Path | None = None, redirect_naar: str | None = None) -> Iterator[_GitHttpServer]:
    server = _GitHttpServer(root, redirect_naar)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(autouse=True)
def _vereist_git() -> None:
    if shutil.which("git") is None or not Path(_http_backend_pad()).exists():
        pytest.skip("git of git-http-backend ontbreekt")


@pytest.fixture
def bare_repo(tmp_path: Path) -> Path:
    """Een bare repo met een commit op main, klaar om te clonen en naar te pushen."""
    root = tmp_path / "repos"
    root.mkdir()
    bron = tmp_path / "bron"
    bron.mkdir()
    _git("init", "--initial-branch=main", cwd=bron)
    _git("config", "user.email", "test@example.com", cwd=bron)
    _git("config", "user.name", "Test", cwd=bron)
    (bron / "README.md").write_text("start\n")
    _git("add", "README.md", cwd=bron)
    _git("commit", "-m", "eerste", cwd=bron)
    _git("clone", "--bare", str(bron), str(root / "repo.git"), cwd=tmp_path)
    return root


@pytest.fixture
def server(bare_repo: Path) -> Iterator[_GitHttpServer]:
    yield from _start_server(root=bare_repo)


def _connector(repo_url: str, werkmap: Path, wachtwoord: str = WACHTWOORD) -> GitConnector:
    return GitConnector(
        repo_url=repo_url,
        working_dir=str(werkmap),
        branch="main",
        username=GEBRUIKER,
        password=f"plain:{wachtwoord}",
    )


async def test_clone_en_push_werken_en_de_clone_draagt_geen_credential(
    server: _GitHttpServer, bare_repo: Path, tmp_path: Path
) -> None:
    werkmap = tmp_path / "werk"
    werkmap.mkdir()
    repo_url = f"{server.basis_url}/repo.git"
    connector = _connector(repo_url, werkmap)

    await connector.ensure_repo_cloned()
    assert (werkmap / "README.md").read_text() == "start\n"

    origin = _git("config", "remote.origin.url", cwd=werkmap)
    assert origin == repo_url
    assert "@" not in origin

    await connector.add_file("nieuw.txt", "inhoud\n")
    await connector.commit_and_push("tweede")

    assert _git("show", "main:nieuw.txt", cwd=bare_repo / "repo.git") == "inhoud"
    assert any(kop.get("Authorization") == VERWACHTE_HEADER for kop in server.ontvangen_headers)


async def test_een_verkeerd_wachtwoord_laat_de_clone_falen(server: _GitHttpServer, tmp_path: Path) -> None:
    """Toont dat de server de auth echt afdwingt, dus dat de geslaagde clone iets bewijst."""
    werkmap = tmp_path / "werk"
    werkmap.mkdir()
    connector = _connector(f"{server.basis_url}/repo.git", werkmap, wachtwoord="onjuist")

    with pytest.raises(RuntimeError, match="Failed to clone repository"):
        await connector.ensure_repo_cloned()


async def test_een_redirect_naar_een_andere_host_krijgt_geen_authorization(
    server: _GitHttpServer, tmp_path: Path
) -> None:
    werkmap = tmp_path / "werk"
    werkmap.mkdir()
    # localhost en 127.0.0.1 zijn voor git twee hosts, dus de sleutel van de doorsturende
    # host dekt het doel niet.
    doel = f"http://localhost:{server.server_address[1]}"
    doorsturen = _start_server(redirect_naar=doel)
    omleider = next(doorsturen)
    try:
        connector = _connector(f"{omleider.basis_url}/repo.git", werkmap)
        with pytest.raises(RuntimeError, match="Failed to clone repository"):
            await connector.ensure_repo_cloned()
    finally:
        next(doorsturen, None)

    assert omleider.ontvangen_headers, "de omleider kreeg geen enkel verzoek"
    assert server.ontvangen_headers, "de tweede host kreeg geen enkel verzoek"
    assert all("Authorization" not in kop for kop in server.ontvangen_headers)
