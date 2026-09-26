"""Eén secret opnieuw vullen vanuit zijn template, veld voor veld.

Dit is de ontbrekende ingang naast `_generate-secrets-shared` in de Taskfile. Die task maakt
een secret alleen aan als het er nog NIET staat, want overschrijven zou elk veld erin roteren.
Wie één wachtwoord wil vervangen kon daardoor alleen het hele `.sops.yaml`-bestand weggooien
en alles tegelijk laten hergenereren. Deze module doet hetzelfde werk per veld.

De template is de bron van de VELDEN en van de `@secret-gen`-annotaties; het bestaande
versleutelde bestand is de bron van de WAARDEN die blijven staan. Daarom kent `Choice` naast
GENERATE en ENTER ook KEEP: zonder die derde keuze roteert het vervangen van één wachtwoord de
andere velden in hetzelfde secret mee, en dat is precies wat de task hierboven vermijdt.

De sleutel komt uit de recipient van het bestaande bestand, niet uit een vlag. Zie `key_for()`:
`task encrypt-secret` leest `security/key.txt` hard, en versleutelt een sandbox-secret dus op
de productiesleutel zonder dat iemand het ziet.
"""

from __future__ import annotations

import base64
import os
import re
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

# De sleutels, de recipients en het ontsleutelen zitten al in de rotatiemotor. Ze hier
# overschrijven zou een tweede set opleveren die stil uit elkaar groeit met de eerste.
# `read_key` zoekt de marker in plaats van regel 3, `public_key_of` leidt de publieke helft af
# met `age-keygen -y` in plaats van de comment te vertrouwen, en `sops_plaintext` gaat door
# `opi.utils.sops.decrypt_sops_with_key`.
from key_rotation import (  # type: ignore[reportMissingImports]
    MissingKey,
    public_key_of,
    read_key,
    sops_plaintext,
)
from ruamel.yaml import YAML

ANNOTATION = re.compile(r"#\s*@secret-gen:(?P<body>\S+)")

#: De repo-root, zodat het script vanuit elke map draait. `scripts/` ligt er direct onder.
REPO = Path(__file__).resolve().parent.parent

SECURITY_DIR = REPO / "security"
ENV_PREFIX = ".env-taskfile-"

# De twee bomen die `_generate-secrets-shared` kent, met de env-variabele die de doelmap noemt.
TREES = {
    "infrastructure": (
        REPO / "infrastructure/bootstrap/infrastructure/secrets/templates",
        REPO / "infrastructure/bootstrap/infrastructure/secrets/config/overlays",
        "INFRASTRUCTURE_CLUSTER_FOLDER",
    ),
    "bootstrap": (
        REPO / "bootstrap/rig-system/kustomize/secrets/templates",
        REPO / "bootstrap/rig-system/kustomize/overlays",
        "BOOTSTRAP_CLUSTER_FOLDER",
    ),
}


class EditFailed(RuntimeError):
    """Iets klopte niet en doorgaan zou een secret beschadigen."""


class Choice(StrEnum):
    KEEP = "keep"
    GENERATE = "generate"
    ENTER = "enter"
    OMIT = "omit"  # het veld komt niet in het secret


@dataclass(frozen=True)
class Field:
    """Een veld onder `stringData`, met wat de template erover zegt."""

    name: str
    kind: str | None  # "random", "bcrypt", "skip", of None als er geen annotatie staat
    length: int | None

    @property
    def generatable(self) -> bool:
        return self.kind in ("random", "bcrypt")

    def describe(self) -> str:
        if self.kind == "skip":
            return "@secret-gen:skip, alleen met de hand"
        if self.kind is None:
            return "geen annotatie, alleen met de hand"
        return f"{self.kind}:{self.length}"


@dataclass(frozen=True)
class Cluster:
    """Een cluster uit een `.env-taskfile-*`, met de doelmap per boom."""

    name: str
    namespace: str
    folders: dict[str, str]


def clusters() -> list[Cluster]:
    """De clusters uit de `.env-taskfile-*`-bestanden, zonder `-current` (dat is een kopie)."""
    found: list[Cluster] = []
    for path in sorted(REPO.glob(f"{ENV_PREFIX}*")):
        name = path.name[len(ENV_PREFIX) :]
        if name == "current":
            continue
        values: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
        folders = {mode: values.get(var, "") for mode, (_, _, var) in TREES.items()}
        found.append(Cluster(name=name, namespace=values.get("RIG_NAMESPACE", ""), folders=folders))
    return found


def templates(directory: str | Path) -> list[Path]:
    """De templates die een secret zijn.

    Dezelfde filter als `_generate-secrets-shared`: `kustomization.yaml` en `README.md` staan in
    dezelfde map en zijn geen secret.
    """
    return sorted(
        path
        for path in Path(directory).glob("*.yaml")
        if path.name.endswith("-secret.yaml") or path.name.endswith(".secret.yaml")
    )


def fields_of(text: str) -> list[Field]:
    """De velden onder `stringData`, in de volgorde van de template.

    Leest de annotatie van de regel zelf, zoals de Taskfile dat doet. Een veld zonder
    annotatie hoort er wel bij: het is een veld dat je met de hand mag zetten, en het
    weglaten zou het onzichtbaar maken in het overzicht.

    **Geef hier een TEMPLATE aan en nooit een ontsleuteld secret.** SOPS bewaart comments als
    losse knopen en zet ze bij het ontsleutelen één veld verderop terug: in een ontsleuteld
    `keycloak-admin-secret.yaml.sops.yaml` staat `@secret-gen:random:16` boven
    KEYCLOAK_ADMIN_PASSWORD in plaats van erachter, en dan leest KEYCLOAK_ADMIN als random:16.
    De template gaat nooit door SOPS heen en is daarom de enige betrouwbare bron.
    `test_secret_edit.py` pint dat verschil vast.
    """
    yaml = YAML()
    data = yaml.load(text)
    if not data or "stringData" not in data:
        return []
    string_data = data["stringData"]

    found: list[Field] = []
    for name in string_data:
        kind: str | None = None
        length: int | None = None
        comment = _comment_on(string_data, name)
        if comment and (match := ANNOTATION.search(comment)):
            body = match.group("body")
            kind = body.split(",")[0].split(":")[0]
            part = body.split(",")[0].split(":")
            if len(part) > 1 and part[1].isdigit():
                length = int(part[1])
        found.append(Field(name=str(name), kind=kind, length=length))
    return found


def _comment_on(mapping: object, key: object) -> str | None:
    """De comment achter één sleutel, zoals ruamel hem bewaart."""
    items = getattr(mapping, "ca", None)
    if items is None:
        return None
    token = items.items.get(key)
    if not token:
        return None
    return "".join(part.value for part in token if part is not None and hasattr(part, "value"))


def generate(kind: str, length: int | None) -> str:
    """Een nieuwe waarde, met dezelfde regel als `_generate-secrets-shared`.

    Het formaat moet gelijk blijven aan wat de generatie-task maakt, anders krijgt een
    geroteerd veld een andere tekenset dan hetzelfde veld op een vers cluster.
    """
    if kind == "random":
        size = length or 16
        out = ""
        while len(out) < size:
            out += base64.b64encode(os.urandom(size)).decode().translate(str.maketrans("", "", "=+/"))
        return out[:size]
    if kind == "bcrypt":
        plain = generate("random", length or 16)
        return bcrypt_hash(plain)
    raise EditFailed(f"onbekend type: {kind}")


def bcrypt_hash(plain: str) -> str:
    """`htpasswd -nbBC 10` met een lege gebruikersnaam, net als de Taskfile."""
    process = subprocess.run(  # noqa: S603
        ["htpasswd", "-nbBC", "10", "", plain],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if process.returncode != 0:
        raise EditFailed(f"htpasswd faalde: {process.stderr.strip()}")
    return process.stdout.strip().lstrip(":").replace("$2b", "$2y")


def key_for(recipient: str) -> Path:
    """Het sleutelbestand in `security/` dat bij deze recipient hoort.

    Zoeken op de publieke sleutel in plaats van een vaste `security/key.txt` is wat voorkomt
    dat een sandbox-secret op de productiesleutel wordt teruggeschreven: `task encrypt-secret`
    leest dat pad hard en zwijgt erover.
    """
    for path in sorted(SECURITY_DIR.glob("*.txt")):
        try:
            if public_key_of(read_key(path)) == recipient:
                return path
        except MissingKey:
            continue  # geen AGE-sleutel; security/ houdt ook tokens
    raise EditFailed(f"geen sleutel in {SECURITY_DIR}/ hoort bij recipient {recipient}")


def decrypt(path: str | Path, private_key: str) -> str:
    """De ontsleutelde inhoud van een SOPS-bestand."""
    plaintext = sops_plaintext(path, private_key)
    if plaintext is None:
        raise EditFailed(f"kon {path} niet ontsleutelen met deze sleutel")
    return plaintext


def values_of(text: str) -> dict[str, str]:
    """De waarden onder `stringData` van een ontsleuteld secret."""
    yaml = YAML()
    data = yaml.load(text)
    if not data or "stringData" not in data:
        return {}
    return {str(name): str(value) for name, value in data["stringData"].items()}


def apply(template_text: str, values: dict[str, str], namespace: str) -> str:
    """De template met de nieuwe waarden erin, comments en volgorde ongemoeid.

    De `@secret-gen`-annotaties moeten blijven staan: ze zijn de enige plek waar staat hoe een
    veld opnieuw gemaakt moet worden. Het bestaande `keycloak-admin-secret.yaml.yaml.sops.yaml`
    is ze bij een handbewerking kwijtgeraakt, en daardoor weet niets meer dat
    KEYCLOAK_ADMIN_PASSWORD een random:16 is.
    """
    yaml = YAML()
    yaml.preserve_quotes = True
    data = yaml.load(template_text)

    # `values` is de VOLLEDIGE inhoud van stringData. Een veld dat er niet in staat gaat eruit,
    # want anders blijft de placeholder van de template staan ("changeMe123!") en belandt die in
    # een secret. Dat is geen theoretisch geval: het odcn-secret kent vier van de zes velden van
    # deze template niet, dus bij een gewone wachtwoordronde blijven die vier ongekozen.
    for name in [name for name in data["stringData"] if name not in values]:
        del data["stringData"][name]
    for name, value in values.items():
        data["stringData"][name] = value

    if namespace:
        data.setdefault("metadata", {})["namespace"] = namespace

    from io import StringIO

    stream = StringIO()
    yaml.dump(data, stream)
    return stream.getvalue()


def encrypt(plaintext: str, destination: str | Path, public_key: str) -> None:
    """Het gevulde secret versleuteld wegschrijven.

    Via stdin, zodat de leesbare versie nooit op schijf staat. `task encrypt-secret` schrijft
    hem wel uit en verwijdert hem daarna, en dat laat hem achter als de stap ertussen faalt.
    """
    process = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "sops",
            "--disable-version-check",
            "--encrypt",
            "--output-type",
            "yaml",
            "--input-type",
            "yaml",
            "--age",
            public_key,
            "/dev/stdin",
        ],
        input=plaintext,
        capture_output=True,
        text=True,
        check=False,
    )
    if process.returncode != 0:
        raise EditFailed(f"sops encrypt faalde: {process.stderr.strip()}")
    Path(destination).write_text(process.stdout, encoding="utf-8")


def encrypted_name(template: str | Path) -> str:
    """De naam die het versleutelde bestand van deze template krijgt.

    `<naam>.yaml` wordt `<naam>.yaml.sops.yaml`, want dat is waar `decrypt-sops.yaml` in elke
    overlay naar verwijst. Eén plek voor deze regel, zodat de round-trip in de Taskfile en dit
    script niet uit elkaar kunnen lopen.
    """
    return f"{Path(template).name}.sops.yaml"


def plain_name(encrypted: str | Path) -> str:
    """De leesbare naam van een versleuteld bestand: de inverse van `encrypted_name()`.

    `keycloak-admin-secret.yaml.sops.yaml` wordt `keycloak-admin-secret.yaml`, niet
    `keycloak-admin-secret.yaml.yaml`. Die tweede is hoe
    `keycloak-admin-secret.yaml.yaml.sops.yaml` ontstond: `task decrypt-secret` verving
    `.sops.yaml` door `.yaml` op een naam die al op `.yaml` eindigde, en `task encrypt-secret`
    plakte daar zijn eigen `.yaml.sops.yaml` achter.
    """
    name = Path(encrypted).name
    if not name.endswith(".sops.yaml"):
        raise EditFailed(f"{name} eindigt niet op .sops.yaml")
    stripped = name[: -len(".sops.yaml")]
    return stripped if stripped.endswith(".yaml") else f"{stripped}.yaml"
