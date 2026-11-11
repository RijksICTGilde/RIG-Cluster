"""Gedeelde hulp voor de groep ``minio_live``: OPI tegen een echte MinIO.

`task test-minio` start de image die het platform pint, met de ``mc`` die de OPI-image
meebrengt, en zet de adressen in de omgeving. Het terugleeskanaal is ``mc`` zelf, los van de
connector onder toets: een returnwaarde van de connector zegt niets over wat de server deed.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from tests.integration.live import MINIO

#: De alias waarop OPI zijn eigen MinIO aanspreekt, zoals de MinioManager hem noemt.
ADMIN_ALIAS = "default-minio"

#: Een geheime sleutel voor de wegwerpgebruikers van deze groep: plaatshoudervorm, en hij
#: bereikt alleen de MinIO waar deze groep tegen meet.
USER_SECRET = "example-Secret-Key-1234"
OTHER_SECRET = "example-Secret-Key-5678"


def host() -> str:
    """``host:poort`` van de wegwerpserver, of een skip (in CI een fout)."""
    return MINIO.omgeving("MINIO_LIVE_HOST")


def admin_access_key() -> str:
    return MINIO.omgeving("MINIO_LIVE_ACCESS_KEY")


def admin_secret_key() -> str:
    return MINIO.omgeving("MINIO_LIVE_SECRET_KEY")


def assert_local() -> None:
    MINIO.assert_local(host())


@dataclass(frozen=True)
class Mc:
    """Het terugleeskanaal: ``mc`` met zijn eigen alias, naast de connector.

    Een eigen alias per rol, want de vraag is niet alleen wat er staat maar ook wie erbij
    komt: met de sleutels van een deployment moet schrijven in de eigen bucket lukken en in
    een andere niet.
    """

    alias: str
    access_key: str
    secret_key: str

    @property
    def env(self) -> dict[str, str]:
        url = f"http://{quote(self.access_key, safe='')}:{quote(self.secret_key, safe='')}@{host()}"
        return {**os.environ, f"MC_HOST_{self.alias}": url}

    def run(self, *args: str, stdin: str | None = None) -> tuple[str, int]:
        done = subprocess.run(
            ["mc", *args],
            capture_output=True,
            text=True,
            env=self.env,
            input=stdin,
            timeout=60,
            check=False,
        )
        return (done.stdout.strip() or done.stderr.strip()), done.returncode

    def ok(self, *args: str, stdin: str | None = None) -> str:
        output, code = self.run(*args, stdin=stdin)
        assert code == 0, f"mc {' '.join(args)}: {output}"
        return output

    def json_lines(self, *args: str) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.ok(*args).splitlines() if line.strip()]

    def put(self, bucket: str, name: str, content: str) -> tuple[str, int]:
        return self.run("pipe", f"{self.alias}/{bucket}/{name}", stdin=content)

    def read(self, bucket: str, name: str) -> str:
        return self.ok("cat", f"{self.alias}/{bucket}/{name}")

    def objects(self, bucket: str) -> set[str]:
        return {entry["key"] for entry in self.json_lines("ls", "--json", f"{self.alias}/{bucket}")}

    def policies_of(self, username: str) -> set[str]:
        info = self.json_lines("admin", "user", "info", self.alias, username, "--json")
        names = {name for entry in info for name in str(entry.get("policyName", "")).split(",") if name}
        return names

    def lifecycle_rule_ids(self, bucket: str) -> set[str]:
        output, code = self.run("ilm", "rule", "ls", "--json", f"{self.alias}/{bucket}")
        if code != 0:
            return set()
        return {
            rule.get("ID", "")
            for entry in (json.loads(line) for line in output.splitlines() if line.strip())
            for rule in entry.get("config", {}).get("Rules", [])
        }


def admin_mc() -> Mc:
    return Mc(alias="readback", access_key=admin_access_key(), secret_key=admin_secret_key())


def drop_by_prefix(prefix: str) -> None:
    """Haal elke bucket en gebruiker met dit voorvoegsel weg, ook die van een afgebroken run.

    De grendel staat hier en niet alleen in de fixture: een toets die geen connector vraagt
    komt daar niet langs, en dan zou alleen de volgorde van de fixtures hem aanroepen.
    """
    assert_local()
    mc = admin_mc()
    for entry in mc.json_lines("ls", "--json", mc.alias):
        name = str(entry.get("key", "")).strip("/")
        if name.startswith(prefix):
            mc.run("rb", "--force", f"{mc.alias}/{name}")
    for entry in mc.json_lines("admin", "user", "list", mc.alias, "--json"):
        name = str(entry.get("accessKey", ""))
        if name.startswith(prefix):
            mc.run("admin", "user", "remove", mc.alias, name)
    for entry in mc.json_lines("admin", "policy", "list", mc.alias, "--json"):
        name = str(entry.get("policy", ""))
        if name.startswith(prefix):
            mc.run("admin", "policy", "remove", mc.alias, name)
