"""De vorm van elk `weed shell`-commando, en wat de connector een fout noemt.

Met een nep-subprocess dat argv en stdin vastlegt: de hele isolatie van deze proef hangt
aan het beleidsdocument en aan de vraag of een geheim buiten argv blijft.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import pytest
from opi.connectors.minio_mc import MinioConnector
from opi.connectors.seaweedfs_shell import (
    JWT_SLEUTEL_ENV,
    SeaweedfsConfigurationError,
    SeaweedfsConnector,
    SeaweedfsExecutionError,
    SeaweedfsValidationError,
    create_seaweedfs_connector,
)
from opi.core.config import settings
from opi.utils.s3_policy import ALL_BUCKET_PERMISSIONS, bucket_access_policy


class NepProces:
    def __init__(self, uitvoer: tuple[bytes, bytes], code: int, hangt: bool = False) -> None:
        self._uitvoer = uitvoer
        self._hangt = hangt
        self.returncode = code
        self.stdin_gezien: bytes | None = None
        self.bestand: Any = None
        self.gedood = False

    def kill(self) -> None:
        self.gedood = True

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:  # noqa: A002
        self.stdin_gezien = input
        if self._hangt:
            # Zoals weed shell zonder bereikbare master: wachten zonder ooit te antwoorden.
            await asyncio.Event().wait()
        # Een -file-pad bestaat alleen zolang het commando loopt, dus hier lezen.
        commando = (input or b"").decode()
        if " -file " in commando:
            with open(commando.split(" -file ")[1].strip()) as bestand:
                self.bestand = json.load(bestand)
        return self._uitvoer


class Shell:
    """De opgevangen aanroepen, plus het antwoord dat het nep-proces teruggeeft."""

    def __init__(self) -> None:
        self.aanroepen: list[dict[str, Any]] = []
        self.antwoord: dict[str, Any] = {"stdout": b"", "stderr": b"", "code": 0, "hangt": False}

    def __getitem__(self, index: int) -> dict[str, Any]:
        return self.aanroepen[index]

    def __len__(self) -> int:
        return len(self.aanroepen)

    def commando(self, index: int = 0) -> str:
        """Wat er op stdin ging: één regel, en de enige plek waar een geheim mag staan."""
        stdin = self.aanroepen[index]["proces"].stdin_gezien
        assert stdin is not None
        return stdin.decode().strip()

    def commandos(self) -> list[str]:
        return [self.commando(i) for i in range(len(self.aanroepen))]


@pytest.fixture
def shell(monkeypatch: pytest.MonkeyPatch) -> Shell:
    gevangen = Shell()

    async def _nep_exec(*argv: str, env: dict[str, str] | None = None, **_: Any) -> NepProces:
        proces = NepProces(
            (gevangen.antwoord["stdout"], gevangen.antwoord["stderr"]),
            gevangen.antwoord["code"],
            hangt=bool(gevangen.antwoord["hangt"]),
        )
        gevangen.aanroepen.append({"argv": list(argv), "env": env, "proces": proces})
        return proces

    monkeypatch.setattr("asyncio.create_subprocess_exec", _nep_exec)
    monkeypatch.setattr(settings, "SEAWEEDFS_FILER_ADDRESS", "seaweedfs:8888")
    monkeypatch.setattr(settings, "SEAWEEDFS_MASTER_ADDRESS", "seaweedfs:9333")
    return gevangen


class TestVorm:
    async def test_beide_adressen_gaan_mee(self, shell: Shell) -> None:
        # Zonder masteradres hangt weed shell in WaitUntilConnected (rij G18).
        await create_seaweedfs_connector().list_buckets()
        assert shell[0]["argv"] == ["weed", "shell", "-filer=seaweedfs:8888", "-master=seaweedfs:9333"]

    async def test_commando_gaat_over_stdin(self, shell: Shell) -> None:
        await SeaweedfsConnector().create_bucket("t-a", owner="t_a")
        assert shell.commando() == "s3.bucket.create -name t-a -owner t_a"

    async def test_wachtwoord_staat_niet_in_argv(self, shell: Shell) -> None:
        # Een geheim in argv is voor elke andere lezer in de pod te zien in /proc.
        await SeaweedfsConnector().create_user("t_a", "t_a", "Geheim123Geheim123")
        assert "Geheim123Geheim123" not in " ".join(shell[0]["argv"])
        assert "Geheim123Geheim123" in shell.commando()

    async def test_versioning_aan_en_uit(self, shell: Shell) -> None:
        connector = SeaweedfsConnector()
        await connector.set_versioning("t-a", True)
        await connector.set_versioning("t-a", False)
        assert shell.commando() == "s3.bucket.versioning -name t-a -enable"
        assert shell.commando(1) == "s3.bucket.versioning -name t-a -suspend"

    async def test_verwijderen_in_de_vorm_van_de_meting(self, shell: Shell) -> None:
        connector = SeaweedfsConnector()
        await connector.delete_bucket("t-a")
        await connector.delete_user("t_a")
        await connector.detach_policy("p", "t_a")
        await connector.delete_policy("p")
        assert shell.commandos() == [
            "s3.bucket.delete -name t-a",
            "s3.user.delete -name t_a",
            "s3.policy.detach -policy p -user t_a",
            "s3.policy -delete -name p",
        ]

    async def test_buckets_opsommen_leest_tab_gescheiden_regels(self, shell: Shell) -> None:
        # Geen JSON, wel regels die met de naam beginnen (rij G4).
        shell.antwoord["stdout"] = b"t-a\t0\nt-b\t12\n\n"
        assert await SeaweedfsConnector().list_buckets() == ["t-a", "t-b"]

    async def test_versioning_lezen(self, shell: Shell) -> None:
        shell.antwoord["stdout"] = b"Versioning: Enabled"
        assert await SeaweedfsConnector().get_versioning("t-a") is True
        shell.antwoord["stdout"] = b"Versioning: Suspended"
        assert await SeaweedfsConnector().get_versioning("t-a") is False

    async def test_het_kindproces_erft_de_platformgeheimen_niet(
        self, shell: Shell, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # RC-279: met os.environ.copy() stond elk beheerwachtwoord in /proc van weed.
        monkeypatch.setenv("DATABASE_ADMIN_PASSWORD", "geheim")
        monkeypatch.setenv(JWT_SLEUTEL_ENV, "de-bearer")

        await SeaweedfsConnector().list_buckets()

        env = shell[0]["env"]
        assert "DATABASE_ADMIN_PASSWORD" not in env
        # weed shell leest de admin-Bearer zelf uit zijn omgeving, dus die ene gaat expliciet mee.
        assert env[JWT_SLEUTEL_ENV] == "de-bearer"

    async def test_zonder_de_bearer_gaat_er_niets_extra_mee(
        self, shell: Shell, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(JWT_SLEUTEL_ENV, raising=False)

        await SeaweedfsConnector().list_buckets()

        assert JWT_SLEUTEL_ENV not in shell[0]["env"]


class TestFouten:
    async def test_bestaat_al_is_geen_uitzondering(self, shell: Shell) -> None:
        # Rij G1 en G8: een tweede aanmaak is exit 1 met "already exists". Zonder dit zou
        # provisioning niet replay-safe zijn.
        shell.antwoord.update({"stderr": b"error: bucket t-a already exists", "code": 1})
        await SeaweedfsConnector().create_bucket("t-a")
        await SeaweedfsConnector().create_user("t_a", "t_a", "Geheim123")

    async def test_bestaat_niet_is_geen_uitzondering_bij_verwijderen(self, shell: Shell) -> None:
        shell.antwoord.update({"stderr": b"error: bucket not found", "code": 1})
        await SeaweedfsConnector().delete_bucket("t-a")
        await SeaweedfsConnector().delete_user("t_a")

    async def test_een_echte_fout_geeft_een_uitzondering(self, shell: Shell) -> None:
        shell.antwoord.update({"stderr": b"error: something broke", "code": 1})
        with pytest.raises(SeaweedfsExecutionError, match="something broke"):
            await SeaweedfsConnector().create_bucket("t-a")

    async def test_een_fout_bij_aanmaken_noemt_het_commando_niet(self, shell: Shell) -> None:
        # pytest schrijft de tekst van een uitzondering in het verslag, en in dat commando
        # staat het wachtwoord.
        shell.antwoord.update({"stderr": b"error: boom", "code": 1})
        with pytest.raises(SeaweedfsExecutionError) as fout:
            await SeaweedfsConnector().create_user("t_a", "t_a", "Geheim123Geheim123")
        assert "Geheim123Geheim123" not in str(fout.value)

    async def test_een_hangend_commando_wordt_een_fout(self, shell: Shell, monkeypatch: pytest.MonkeyPatch) -> None:
        # Een onbereikbare master laat weed shell wachten in WaitUntilConnected zonder
        # ooit te antwoorden (rij G18); de time-out maakt daar een fout van.
        monkeypatch.setattr("opi.connectors.seaweedfs_shell.SHELL_TIMEOUT_SECONDS", 0.05)
        shell.antwoord["hangt"] = True
        with pytest.raises(SeaweedfsExecutionError, match="geen antwoord"):
            await SeaweedfsConnector().list_buckets()
        assert shell[0]["proces"].gedood is True

    async def test_bestaan_van_een_gebruiker_leest_de_exitcode(self, shell: Shell) -> None:
        assert await SeaweedfsConnector().user_exists("t_a") is True
        shell.antwoord["code"] = 1
        assert await SeaweedfsConnector().user_exists("t_a") is False

    async def test_een_ongeldige_naam_komt_niet_bij_het_proces(self, shell: Shell) -> None:
        with pytest.raises(SeaweedfsValidationError):
            await SeaweedfsConnector().create_bucket("Ongeldige_Naam")
        with pytest.raises(SeaweedfsValidationError):
            await SeaweedfsConnector().create_user("123fout", "123fout", "Geheim123")
        assert len(shell) == 0

    async def test_zonder_adressen_weigert_de_connector(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "SEAWEEDFS_FILER_ADDRESS", "")
        monkeypatch.setattr(settings, "SEAWEEDFS_MASTER_ADDRESS", "")
        assert SeaweedfsConnector.is_configured() is False
        with pytest.raises(SeaweedfsConfigurationError):
            await SeaweedfsConnector().list_buckets()


class TestBeleidsdocument:
    """Hetzelfde document als MinIO vandaag bouwt: één isolatie voor twee diensten."""

    async def test_grant_bucket_access_geeft_het_document_van_minio(
        self, shell: Shell, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        van_minio: dict[str, Any] = {}

        async def _nep_create_policy(alias: str, policy_name: str, policy_document: dict[str, Any]) -> dict[str, Any]:
            van_minio["document"] = policy_document
            return {"status": "created"}

        async def _nep_attach(alias: str, policy_name: str, username: str) -> dict[str, Any]:
            return {"status": "attached"}

        minio = MinioConnector()
        monkeypatch.setattr(minio, "create_policy", _nep_create_policy)
        monkeypatch.setattr(minio, "attach_policy", _nep_attach)
        await minio.grant_bucket_access("alias", "t_a", "t-a", ["read", "write", "delete", "list"])

        van_seaweedfs: dict[str, Any] = {}

        async def _nep_put(naam: str, document: dict[str, Any]) -> None:
            van_seaweedfs["naam"] = naam
            van_seaweedfs["document"] = document

        connector = SeaweedfsConnector()
        monkeypatch.setattr(connector, "put_policy", _nep_put)
        await connector.grant_bucket_access("t_a", "t-a", "t_a-t-a-policy")

        assert van_seaweedfs["document"] == van_minio["document"]
        assert van_seaweedfs["naam"] == "t_a-t-a-policy"
        assert shell.commando() == "s3.policy.attach -policy t_a-t-a-policy -user t_a"

    async def test_put_policy_stuurt_het_document_als_bestand(self, shell: Shell) -> None:
        # Het enige commando dat het isolatiedocument naar de server brengt, dus de vorm
        # en de inhoud van het bestand staan hier echt vast.
        document = bucket_access_policy("t-a", ALL_BUCKET_PERMISSIONS)
        await SeaweedfsConnector().put_policy("t_a-t-a-policy", document)

        commando = shell.commando()
        pad = commando.split(" -file ")[1]
        assert commando == f"s3.policy -put -name t_a-t-a-policy -file {pad}"
        assert shell[0]["proces"].bestand == document
        assert not os.path.exists(pad)

    async def test_twee_aanroepen_gebruiken_niet_hetzelfde_bestand(self, shell: Shell) -> None:
        # De taakwerker draait meerdere gangen tegelijk in dezelfde pod. Op een vast pad
        # overschrijft de ene gang het document van de andere, en dan koppelt A de
        # bucket-ARN van B.
        connector = SeaweedfsConnector()
        await connector.put_policy("t_a-t-a-policy", bucket_access_policy("t-a", ALL_BUCKET_PERMISSIONS))
        await connector.put_policy("t_b-t-b-policy", bucket_access_policy("t-b", ALL_BUCKET_PERMISSIONS))

        paden = [commando.split(" -file ")[1] for commando in shell.commandos()]
        assert paden[0] != paden[1]

    async def test_een_ongeldige_policynaam_komt_niet_bij_het_proces(self, shell: Shell) -> None:
        with pytest.raises(SeaweedfsValidationError):
            await SeaweedfsConnector().put_policy("fout beleid", {"Version": "2012-10-17", "Statement": []})
        with pytest.raises(SeaweedfsValidationError):
            await SeaweedfsConnector().delete_policy("fout beleid")
        assert len(shell) == 0
