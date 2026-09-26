"""Eén wachtwoord in een secret vervangen, zonder de rest van het secret te roteren.

    uv run --project operations-manager/python python scripts/edit-secret.py
    uv run --project operations-manager/python python scripts/edit-secret.py --dry-run

Het loopt de templates in `secrets/templates/` af, vraagt welk secret je wilt aanpassen, en
per veld of de waarde blijft staan, opnieuw gegenereerd wordt of door jou wordt opgegeven.
Daarna schrijft het het hele secret opnieuw en versleutelt het met SOPS.

Dit is wat `task generate-infrastructure-secrets-for-cluster` niet kan: die slaat een bestaand
secret over, juist omdat overschrijven élk veld erin zou roteren. Weggooien en opnieuw laten
genereren was tot nu toe het enige alternatief, en dat roteert Keycloak, MinIO en PostgreSQL
tegelijk.

De waarden komen nooit op schijf te staan: ze gaan via stdin naar `sops`. De logica zit in
`secret_edit.py` ernaast; zie `scripts/README.md`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from key_rotation import public_key_of, read_key, sops_recipients  # type: ignore[reportMissingImports]
from secret_edit import (  # type: ignore[reportMissingImports]
    TREES,
    Choice,
    Cluster,
    EditFailed,
    Field,
    apply,
    clusters,
    decrypt,
    encrypt,
    encrypted_name,
    fields_of,
    generate,
    key_for,
    templates,
    values_of,
)

# De regel uit de Taskfile (`KEY_FILE` op regel 892) voor een secret dat nog niet bestaat. Staat
# het bestand er al, dan wint de recipient erin: zie `key_for()`.
DEFAULT_KEYS = {"sandboxed-local": Path("security/sandbox-key.txt")}
FALLBACK_KEY = Path("security/key.txt")


def choose(question: str, options: list[str]) -> int:
    """Eén keuze uit een genummerde lijst, net zo lang tot er een geldige komt."""
    print(f"\n{question}")
    for number, option in enumerate(options, start=1):
        print(f"  {number}. {option}")
    while True:
        answer = input("> ").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return int(answer) - 1
        print(f"Kies 1 tot en met {len(options)}.")


def ask_field(field: Field, current: str | None) -> tuple[Choice, str | None]:
    """Wat er met één veld moet gebeuren.

    KEEP staat alleen in de lijst als er een waarde is om te behouden, en staat dan bovenaan:
    een secret aanpassen betekent bijna altijd één veld aanraken en de rest laten staan.
    """
    options: list[tuple[Choice, str]] = []
    if current is not None:
        options.append((Choice.KEEP, "laat staan"))
    if field.generatable:
        options.append((Choice.GENERATE, f"genereer opnieuw ({field.describe()})"))
    options.append((Choice.ENTER, "zelf opgeven"))

    if len(options) == 1:
        choice = options[0][0]
    else:
        status = "nog geen waarde" if current is None else f"{len(current)} tekens"
        index = choose(f"{field.name} ({field.describe()}, nu: {status})", [label for _, label in options])
        choice = options[index][0]

    if choice is Choice.ENTER:
        while True:
            value = input(f"  waarde voor {field.name}: ").strip()
            if value:
                return choice, value
            print("  Leeg is geen wachtwoord.")
    if choice is Choice.GENERATE:
        return choice, generate(str(field.kind), field.length)
    return choice, current


def pick_secret(cluster: Cluster) -> tuple[str, Path, Path]:
    """Het secret dat bewerkt wordt: de boom, de template en het doelbestand."""
    options: list[tuple[str, Path, Path]] = []
    for mode, (templates_dir, output_dir, _) in TREES.items():
        folder = cluster.folders.get(mode, "")
        if not folder:
            continue
        options.extend(
            (mode, template, output_dir / folder / encrypted_name(template)) for template in templates(templates_dir)
        )

    if not options:
        raise EditFailed(f"geen templates gevonden voor cluster {cluster.name}")

    labels = [
        f"{template.stem:<42} [{mode}] {'bestaat' if destination.exists() else 'NIEUW'}"
        for mode, template, destination in options
    ]
    return options[choose("Welk secret wil je aanpassen?", labels)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="toon wat er zou gebeuren, schrijf niets")
    args = parser.parse_args(argv)

    available = clusters()
    cluster = available[choose("Voor welk cluster?", [f"{c.name} (namespace {c.namespace})" for c in available])]

    mode, template, destination = pick_secret(cluster)

    # De sleutel komt uit het bestand zelf als het er al is. Dat is de guard tegen een
    # sandbox-secret dat op de productiesleutel wordt teruggeschreven.
    current: dict[str, str] = {}
    if destination.exists():
        found = sops_recipients(destination)
        if not found:
            raise EditFailed(f"{destination} heeft geen AGE-recipient in zijn SOPS-metadata")
        key_file = key_for(found[0])
        current = values_of(decrypt(destination, read_key(key_file)))
    else:
        key_file = DEFAULT_KEYS.get(cluster.name, FALLBACK_KEY)
        if not key_file.exists():
            raise EditFailed(f"sleutelbestand ontbreekt: {key_file}")

    print(f"\nTemplate:  {template}")
    print(f"Doel:      {destination}")
    print(f"Sleutel:   {key_file} ({public_key_of(read_key(key_file))})")
    print(f"Namespace: {cluster.namespace}")

    values: dict[str, str] = {}
    decisions: list[tuple[str, Choice]] = []
    for field in fields_of(template.read_text(encoding="utf-8")):
        choice, value = ask_field(field, current.get(field.name))
        decisions.append((field.name, choice))
        if value is not None:
            values[field.name] = value

    print("\nDit gaat er gebeuren:")
    for name, choice in decisions:
        label = {Choice.KEEP: "blijft staan", Choice.GENERATE: "NIEUW gegenereerd", Choice.ENTER: "NIEUW opgegeven"}
        print(f"  {name:<34} {label[choice]}")

    if args.dry_run:
        print(f"\n--dry-run: {destination} is niet aangeraakt.")
        return 0

    if not any(choice is not Choice.KEEP for _, choice in decisions):
        print("\nNiets gewijzigd, niets geschreven.")
        return 0

    if input(f"\nSchrijf naar {destination}? [j/N] ").strip().lower() not in ("j", "ja", "y", "yes"):
        print("Afgebroken, niets geschreven.")
        return 1

    destination.parent.mkdir(parents=True, exist_ok=True)
    plaintext = apply(template.read_text(encoding="utf-8"), values, cluster.namespace)
    encrypt(plaintext, destination, public_key_of(read_key(key_file)))
    print(f"✅ {destination}")
    print("\nCommit het bestand; ArgoCD rolt het uit. Een pod die het secret als env leest start niet vanzelf opnieuw.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except EditFailed as failure:
        print(f"❌ {failure}", file=sys.stderr)
        raise SystemExit(1) from failure
    except KeyboardInterrupt:
        print("\nAfgebroken.", file=sys.stderr)
        raise SystemExit(1) from None
