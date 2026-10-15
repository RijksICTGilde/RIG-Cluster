"""Eén wachtwoord in een secret vervangen, zonder de rest van het secret te roteren.

    uv run --project operations-manager/python python scripts/edit-secret.py
    uv run --project operations-manager/python python scripts/edit-secret.py --dry-run
    uv run --project operations-manager/python python scripts/edit-secret.py --check
    uv run --project operations-manager/python python scripts/edit-secret.py --component redis --apply

Twee modi:

- **bewerken** (het oorspronkelijke): loop de templates af, kies een secret, bepaal per
  veld of de waarde blijft staan, opnieuw gegenereerd wordt of door jou wordt opgegeven.
  Daarna schrijft het het hele secret opnieuw en versleutelt het met SOPS. Dit is wat
  `task generate-infrastructure-secrets-for-cluster` niet kan: die slaat een bestaand
  secret over, juist omdat overschrijven élk veld erin zou roteren.

- **roteren** (`--component`, `--all`): eerst de app, dan het bestand. Per component hoort
  een handler in `secret_rotate.py`: live (postgres, redis, keycloak, relay),
  env-restart (minio, pgadmin, prometheus) of external (transip, grafana). Roteren zonder
  `--apply` toont alleen het plan; `--check` meet drift tussen bestand en app zonder iets
  te veranderen.

De waarden komen nooit op schijf te staan: ze gaan via stdin naar `sops`. De logica zit in
`secret_edit.py` (bestand) en `secret_rotate.py` (rotatie) ernaast; zie `scripts/README.md`.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import secret_rotate as rotate  # type: ignore[reportMissingImports]
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
    key_entry_for,
    templates,
    values_of,
)

# De regel uit de Taskfile (`KEY_FILE` op regel 892) voor een secret dat nog niet bestaat. Staat
# het bestand er al, dan wint de recipient erin: zie `key_entry_for()`.
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


def ask_field(field: Field, current: str | None, *, is_new_secret: bool) -> tuple[Choice, str | None]:
    """Wat er met één veld moet gebeuren.

    De standaardkeuze staat bovenaan en verandert niets. Voor een veld dat het secret al heeft is
    dat KEEP; voor een veld dat de TEMPLATE wel kent en het secret niet, is dat OMIT.

    Dat tweede geval is de normale toestand en niet de uitzondering: het odcn-keycloak-secret
    kent twee van de zes velden van zijn template, want KEYCLOAK_ADMIN_CLIENT_SECRET en de drie
    OTP-velden zijn later aan de template toegevoegd. Zonder OMIT zou een ronde die alleen het
    wachtwoord roteert er vier velden bij zetten, en dat is een andere wijziging dan gevraagd.
    Bij een secret dat nog niet bestaat is er niets om weg te laten en vervalt de keuze.
    """
    options: list[tuple[Choice, str]] = []
    if current is not None:
        options.append((Choice.KEEP, "laat staan"))
    elif not is_new_secret:
        options.append((Choice.OMIT, "laat weg (staat nu niet in het secret)"))
    if field.generatable:
        options.append((Choice.GENERATE, f"genereer opnieuw ({field.describe()})"))
    options.append((Choice.ENTER, "zelf opgeven"))

    if len(options) == 1:
        choice = options[0][0]
    else:
        status = "nieuw in de template" if current is None else f"{len(current)} tekens"
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
    if choice is Choice.OMIT:
        return choice, None
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


def guard_production(cluster: Cluster) -> None:
    """Extra drempel voor --apply op productie: typ de clusternaam over.

    `kubectl`-context is NIET de guard: wie op odcn draait met een oude context hoeft
    daar niets van te merken. Het typen van de naam is wat "per ongeluk" uitsluit.
    """
    if "prd" not in cluster.name and "production" not in cluster.name:
        return
    print(f"\n⚠️  Je staat op het punt het cluster van {cluster.name} AAN TE PASSEN.")
    typed = input(f"Typ exact '{cluster.name}' om door te gaan: ").strip()
    if typed != cluster.name:
        raise EditFailed("clusternaam klopt niet; afgebroken")


def rotate_flow(args: argparse.Namespace, cluster: Cluster) -> int:
    """De rotatie-ingang: --check meet drift, zonder --apply toont ze het plan, met --apply
    voert ze het plan uit (per stap een bevestiging)."""
    gaps = rotate.coverage_gaps()
    if gaps:
        raise EditFailed(
            "de dekking-poort is open: " + " | ".join(gaps) + " -- los dit eerst (rij in de tabel of benoemde uitzondering)"
        )
    if args.component and args.component not in rotate.BY_KEY:
        raise EditFailed(f"onbekend component '{args.component}'. Kies uit: {', '.join(rotate.BY_KEY)}")
    if args.rotate_all:
        # Externe rotaties horen niet in bulk: hun sleutelwijziging loopt buiten dit cluster
        # (TransIP-console, token-aanvraag bij ODCN). Die draai je apart, bewust, per stuk.
        selected = [c for c in rotate.COMPONENTS if c.category != "external"]
        excluded = [c.key for c in rotate.COMPONENTS if c.category == "external"]
        if excluded:
            print(f"(externe rotaties horen niet in bulk en zijn apart te draaien: {', '.join(excluded)})")
    elif args.component:
        selected = [rotate.BY_KEY[args.component]]
    else:
        labels = [f"{c.key:<16} {c.title}" for c in rotate.COMPONENTS] + ["--all: alles in de volgorde van de tabel"]
        picked = choose("Welk component roteren?", labels)
        selected = rotate.COMPONENTS if picked == len(labels) - 1 else [rotate.COMPONENTS[picked]]

    apply_changes = bool(args.apply) and not args.dry_run
    if apply_changes:
        guard_production(cluster)

    kube = rotate.Kube(cluster.namespace, dry_run=not apply_changes and not args.check, context=args.context)
    print(f"\nCluster: {cluster.name} (namespace {cluster.namespace})")
    if not kube.dry_run:
        if args.context:
            context = args.context
        else:
            probe = ["kubectl", "config", "current-context"]
            context = subprocess.run(probe, capture_output=True, text=True, check=False).stdout.strip()  # noqa: S603
        print(f"kubectl-context: {context}  <- clusteracties lopen HIER")
    print(
        f"Modus:   {'CHECK (alleen drift meten, leest alleen)' if args.check else 'APPLY' if apply_changes else 'DRY-RUN (alleen het plan)'}"
    )

    failures = 0
    bulk = len(selected) > 1
    ctxs: dict[str, object] = {}
    for component in selected:
        print(f"\n=== {component.title} ===")
        if args.check:
            for line in rotate.check_component(component, cluster, kube):
                print(line)
            continue
        ctx = (
            rotate.gather_ctx(component, cluster, kube, apply_changes=apply_changes)
            if apply_changes
            else rotate.gather_dry_ctx(component, cluster, kube)
        )
        ctxs[component.key] = ctx
        steps = rotate.steps_for(component, cluster, kube, ctx, defer_restarts=bulk)
        if not apply_changes:
            for number, step in enumerate(steps, start=1):
                print(f"  {number}. [{step.phase}] {step.title}")
                for line in step.dry_text:
                    print(f"       {line}")
            continue
        if not rotate.execute(ctx, steps):
            print(f"❌ {component.key} afgebroken; later opnieuw beginnen kan gewoon.")
            failures += 1
            break  # --all: stop bij de eerste breuk, een halve ronde verder kan drift geven
    if bulk and not args.check and not failures:
        # De slotafronding: in een ronde met meerdere onderdelen herstarten we één keer,
        # gededupliceerd (ZAD als laatste), in plaats van per geroteerd wachtwoord.
        restart = rotate.final_restart_step(selected, cluster)
        slot: list[tuple[object, object]] = []  # Step met zijn eigen Ctx
        if restart is not None:
            slot.append((rotate.Ctx(cluster=cluster, kube=kube, current={}, new={}, apply_changes=True), restart))
        for component in selected:
            verify = rotate.env_verify_for(component, cluster)
            if verify is not None and component.key in ctxs:
                slot.append((ctxs[component.key], verify))
        if not slot:
            return 0
        if not apply_changes:
            print("\n=== Slotafronding van de ronde ===")
            for _c, step in slot:
                print(f"  - [{step.phase}] {step.title}")
            return 0
        print("\n=== Slotafronding van de ronde ===")
        for step_ctx, step in slot:
            if not rotate.execute(step_ctx, [step]):
                print("❌ de slotafronding is niet volledig; herstellen kan met een herhaalde ronde.")
                failures += 1
    return 1 if failures else 0


def edit_flow(args: argparse.Namespace, cluster: Cluster) -> int:
    mode, template, destination = pick_secret(cluster)

    # De sleutel komt uit het bestand zelf als het er al is. Dat is de guard tegen een
    # sandbox-secret dat op de productiesleutel wordt teruggeschreven.
    current: dict[str, str] = {}
    private_key: str
    key_label: str
    if destination.exists():
        found = sops_recipients(destination)
        if not found:
            raise EditFailed(f"{destination} heeft geen AGE-recipient in zijn SOPS-metadata")
        entry = key_entry_for(found[0])
        private_key = entry.private
        key_label = f"{entry.source} ({entry.public})"
        current = values_of(decrypt(destination, private_key))
    else:
        key_file = DEFAULT_KEYS.get(cluster.name, FALLBACK_KEY)
        if not key_file.exists():
            raise EditFailed(f"sleutelbestand ontbreekt: {key_file}")
        private_key = read_key(key_file)
        key_label = f"{key_file} ({public_key_of(private_key)})"

    print(f"\nTemplate:  {template}")
    print(f"Doel:      {destination}")
    print(f"Sleutel:   {key_label}")
    print(f"Namespace: {cluster.namespace}")

    is_new_secret = not destination.exists()
    values: dict[str, str] = {}
    decisions: list[tuple[str, Choice]] = []
    for field in fields_of(template.read_text(encoding="utf-8")):
        choice, value = ask_field(field, current.get(field.name), is_new_secret=is_new_secret)
        decisions.append((field.name, choice))
        if value is not None:
            values[field.name] = value

    print("\nDit gaat er gebeuren:")
    for name, choice in decisions:
        label = {
            Choice.KEEP: "blijft staan",
            Choice.OMIT: "blijft weg",
            Choice.GENERATE: "NIEUW gegenereerd",
            Choice.ENTER: "NIEUW opgegeven",
        }
        print(f"  {name:<34} {label[choice]}")

    if args.dry_run:
        print(f"\n--dry-run: {destination} is niet aangeraakt.")
        return 0

    if not any(choice not in (Choice.KEEP, Choice.OMIT) for _, choice in decisions):
        print("\nNiets gewijzigd, niets geschreven.")
        return 0

    # Default JA, zoals in de rotatieflow: je hebt hierboven per veld al gekozen en het
    # overzicht staat erbij, dus dit is een bevestiging en geen tweede beslissing. De
    # barriere tegen het verkeerde cluster blijft wel staan: die vraagt de clusternaam.
    if input(f"\nSchrijf naar {destination}? [J/n] ").strip().lower() in ("n", "nee", "no"):
        print("Afgebroken, niets geschreven.")
        return 1

    destination.parent.mkdir(parents=True, exist_ok=True)
    plaintext = apply(template.read_text(encoding="utf-8"), values, cluster.namespace)
    encrypt(plaintext, destination, private_key)
    print(f"✅ {destination}")
    print("\nCommit het bestand; ArgoCD rolt het uit. Een pod die het secret als env leest start niet vanzelf opnieuw.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="toon wat er zou gebeuren, schrijf niets")
    parser.add_argument(
        "--check", action="store_true", help="meet drift tussen bestand en app, zonder iets te wijzigen"
    )
    parser.add_argument("--component", help="roteer alleen dit component (zie secret_rotate.BY_KEY)")
    parser.add_argument(
        "--all", dest="rotate_all", action="store_true", help="roteer alle componenten in tabel-volgorde"
    )
    parser.add_argument("--apply", action="store_true", help="voer de rotatie echt uit (zonder deze vlag: plan tonen)")
    parser.add_argument("--context", help="kubectl-context gebruiken in plaats van de huidige")
    parser.add_argument(
        "--seed-overlay",
        action="store_true",
        help="maak de secrets-overlay van dit cluster in deze repo, gevoed met de live waarden "
        "(voor een cluster waarvan de overlay nog ontbreekt, zoals sandboxed-local)",
    )
    args = parser.parse_args(argv)

    available = clusters()
    cluster = available[choose("Voor welk cluster?", [f"{c.name} (namespace {c.namespace})" for c in available])]

    if args.seed_overlay:
        # Lezen is read-only; de dry-run-guard hoort bij de SCHRIJFstappen (bestanden),
        # niet bij het ophalen van de live waarden waaruit het plan volgt.
        kube = rotate.Kube(cluster.namespace, dry_run=False, context=args.context)
        print(f"\nCluster: {cluster.name} (namespace {cluster.namespace})")
        return rotate.seed_overlay(cluster, kube, apply_changes=bool(args.apply))

    wants_rotation = args.check or args.component or args.rotate_all
    if not wants_rotation:
        mode = choose(
            "Wat wil je doen?",
            [
                "Secret-velden bewerken (alleen het bestand)",
                "Een wachtwoord roteren (app én bestand)",
                "Alleen drift meten (check)",
            ],
        )
        wants_rotation = mode != 0
        args.check = args.check or mode == 2

    return rotate_flow(args, cluster) if wants_rotation else edit_flow(args, cluster)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except EditFailed as failure:
        print(f"❌ {failure}", file=sys.stderr)
        raise SystemExit(1) from failure
    except KeyboardInterrupt:
        print("\nAfgebroken.", file=sys.stderr)
        raise SystemExit(1) from None
