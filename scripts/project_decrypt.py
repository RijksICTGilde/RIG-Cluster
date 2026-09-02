#!/usr/bin/env python3
"""Schrijf een projectbestand uit met alle AGE-versleutelde waarden leesbaar.

Een projectbestand is grotendeels onleesbaar: de ``user-env-vars`` per component, de
bijlagen (headscale ``config.yaml``, ``acl.json``), het repository-wachtwoord, de api-key
en de projectsleutel zelf staan er versleuteld in. Wie wil nagaan hoe een project is
ingericht, of documentatie ertegen wil controleren, kan dat zo niet.

Dit script doet één ding: het leest een projectbestand, ontsleutelt alles wat het kan, en
schrijft het resultaat naar stdout. Commentaar en sleutelvolgorde blijven staan, want het
laadt en schrijft via ``opi.utils.yaml_util``, de canonieke round-trip-schrijver.

    uv run python ../../scripts/project_decrypt.py <sleutel> <projectbestand> > uit.yaml

De sleutel is de **systeemsleutel** (``security/key.txt``, of de sandbox-variant), niet de
projectsleutel. Die laatste staat versleuteld in het bestand zelf en wordt hier afgeleid.
Daarom lopen er twee rondes over de boom: eerst met de projectsleutel, dan met de
systeemsleutel voor de waarden die daar direct onder vallen. Wat met geen van beide
opengaat blijft ongemoeid staan, zodat je aan de uitvoer ziet dat het niet gelukt is.

LET OP: de uitvoer bevat platte geheimen. Schrijf hem niet weg in een git-werkmap.
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Make ``opi`` importable no matter the working directory: the OPI package lives
# in operations-manager/python, a sibling of this scripts/ directory.
_OPI_ROOT = Path(__file__).resolve().parents[1] / "operations-manager" / "python"
if str(_OPI_ROOT) not in sys.path:
    sys.path.insert(0, str(_OPI_ROOT))

from opi.utils.age import (  # noqa: E402  (after sys.path bootstrap above)
    decrypt_age_block_to_bytes,
    decrypt_age_content,
    decrypt_tree,
    is_age_encrypted,
)
from opi.utils.yaml_util import dump_yaml_to_string, load_yaml_from_string  # noqa: E402

AGE_KEY_MARKER = "AGE-SECRET-KEY-"


def read_key(argument: str) -> str:
    """Neem de sleutel uit een bestand, of gebruik het argument zelf als sleutel.

    ``security/key.txt`` is de uitvoer van ``age-keygen``: twee commentaarregels en dan de
    sleutel. Zoeken op de marker is robuuster dan de derde regel pakken, want dat laatste
    breekt zodra er een regel bijkomt.
    """
    if argument.startswith(AGE_KEY_MARKER):
        return argument.strip()

    path = Path(argument)
    if not path.is_file():
        raise SystemExit(f"Sleutel is geen bestand en ziet er niet uit als een AGE-sleutel: {argument}")

    for line in path.read_text().splitlines():
        if line.strip().startswith(AGE_KEY_MARKER):
            return line.strip()

    raise SystemExit(f"Geen regel met {AGE_KEY_MARKER} gevonden in {argument}")


async def decode_attachments(data: object, private_key: str) -> None:
    """Maak bijlagen leesbaar: die dragen een extra base64-laag onder de AGE-laag.

    ``encrypt_file_to_age_block`` base64't de bytes voordat het versleutelt, zodat een
    binair bestand de string-gebaseerde helpers overleeft. De gewone boomwandeling haalt
    er dus base64 uit in plaats van de inhoud. Herkenning gaat op vorm (een dict met
    ``filename`` en ``content``) in plaats van op een vast pad, want een projectbestand
    kan bijlagen op meer dan een plek dragen.
    """
    if isinstance(data, dict):
        content = data.get("content")
        if "filename" in data and isinstance(content, str) and is_age_encrypted(content):
            try:
                raw = await decrypt_age_block_to_bytes(content, private_key)
            except Exception as e:
                print(f"Bijlage '{data.get('filename')}' gaat niet open ({e})", file=sys.stderr)
                return
            try:
                data["content"] = raw.decode("utf-8")
            except UnicodeDecodeError:
                data["content"] = f"<binair bestand, {len(raw)} bytes>"
            return
        for value in data.values():
            await decode_attachments(value, private_key)
    elif isinstance(data, list):
        for item in data:
            await decode_attachments(item, private_key)


async def decrypt_project(data: dict, system_key: str) -> None:
    """Ontsleutel de boom in plaats, eerst met de projectsleutel en dan met de systeemsleutel."""
    encrypted_project_key = data.get("config", {}).get("age-private-key")
    if encrypted_project_key:
        try:
            project_key = await decrypt_age_content(encrypted_project_key, system_key)
            await decode_attachments(data, project_key)
            await decrypt_tree(data, project_key, include_prefixed=True)
        except Exception as e:
            # Geen reden om te stoppen: de sleutel die je meegaf kan de projectsleutel
            # zelf zijn in plaats van de systeemsleutel. De ronde hieronder probeert hem
            # alsnog rechtstreeks op de boom.
            print(f"Projectsleutel gaat niet open met deze sleutel ({e}).", file=sys.stderr)
            print("Ik probeer de meegegeven sleutel nu rechtstreeks op het bestand.", file=sys.stderr)
    else:
        print("Waarschuwing: geen config.age-private-key in dit bestand", file=sys.stderr)

    await decode_attachments(data, system_key)
    await decrypt_tree(data, system_key, include_prefixed=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("key", help="pad naar de systeemsleutel (bv. security/key.txt), of de sleutel zelf")
    parser.add_argument("project_file", help="pad naar het projectbestand")
    args = parser.parse_args()

    logging.disable(logging.CRITICAL)

    data = load_yaml_from_string(Path(args.project_file).read_text())
    if data is None:
        raise SystemExit(f"Kon {args.project_file} niet als YAML lezen")

    asyncio.run(decrypt_project(data, read_key(args.key)))
    sys.stdout.write(dump_yaml_to_string(data))


if __name__ == "__main__":
    main()
