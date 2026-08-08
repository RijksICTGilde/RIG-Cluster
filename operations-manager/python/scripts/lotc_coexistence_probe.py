"""Meet of LOTC en jinja-roos-components in een applicatie naast elkaar kunnen bestaan.

Aanleiding: het componentenplan (``plans/naar-het-nieuwe-componentensysteem.md``). Het
einddoel is dat LOTC jinja-roos vervangt, maar de omzetting is een POC naast een lopende
release. Dan is dit de vraag die alles bepaalt: kan die proef pagina voor pagina, of is de
kleinste eerste stap meteen de hele applicatie?

Beide systemen doen hetzelfde: ze registreren een Jinja-extensie die ``<c-*>``-tags
voorbewerkt, en ze hangen hun componenttemplates aan ``loader.searchpath``. Deze proef
meet wat er gebeurt als je ze allebei aanzet, in beide volgordes, en wat er gebeurt als je
ze in twee losse omgevingen zet.

Dit script draait NIET in de test-suite: LOTC is geen dependency van dit project (het staat
alleen op de interne Forgejo, zie het rapport). Het is er om de meting na te doen.

Draaien::

    git clone http://localhost:3000/robbert/lord-of-the-components.git lotc
    cd lotc && git checkout plan-v7-lotc-thema-agnostische-compiler-performanc
    uv venv probe-venv --python 3.14
    VIRTUAL_ENV=$PWD/probe-venv uv pip install -e python -e packages/lotc-rvo \
        -e packages/lotc-nldd -e packages/lotc-layout -e packages/lotc-forms \
        beautifulsoup4 lxml
    # jinja-roos-components erbij (git-dependency van dit project)
    ./probe-venv/bin/python /workspace/operations-manager/python/scripts/lotc_coexistence_probe.py

De uitkomst staat vast in ``docs/lotc-samenleven-met-jinja-roos.md``.
"""

import pathlib
import tempfile

import jinja_roos_components
import lord_of_the_components
from jinja2 import Environment, FileSystemLoader

# De thema's in de volgorde die het LOTC-project vastlegde: lotc-forms als laatste, na het
# visuele thema, anders lossen de invoervelden niet op.
DESIGN_SYSTEMS = ["lotc-layout", "nldd", "lotc-forms"]

# Een tag die alleen roos kent, een die alleen LOTC kent, en een die beide kennen.
ROOS_ONLY = '<c-secret-field fieldId="k" value="geheim"></c-secret-field>'
LOTC_ONLY = '<c-stack gap="md"><c-paragraph>hoi</c-paragraph></c-stack>'
BEIDE = '<c-heading type="h1">Titel</c-heading>'

WERKMAP = pathlib.Path(tempfile.mkdtemp())


def _zet_op(env: Environment, systeem: str, **kwargs: object) -> None:
    if systeem == "roos":
        jinja_roos_components.setup_components(env, **kwargs)  # type: ignore[arg-type]
    else:
        lord_of_the_components.setup_components(env, design_systems=DESIGN_SYSTEMS, **kwargs)  # type: ignore[arg-type]


def meet(label: str, bron: str, systemen: list[tuple[str, dict]]) -> None:
    """Zet de systemen in deze volgorde aan en render de bron; print wat eruit komt."""
    (WERKMAP / "t.html.j2").write_text(bron)
    # Een echte FileSystemLoader, want beide systemen hangen hun componenttemplates aan
    # loader.searchpath. Met een DictLoader lijkt elk component "niet geimplementeerd".
    env = Environment(loader=FileSystemLoader(str(WERKMAP)), autoescape=True)
    try:
        for naam, kwargs in systemen:
            _zet_op(env, naam, **kwargs)
        uitvoer = env.get_template("t.html.j2").render()
        print(f"  {label:<34} OK    {' '.join(uitvoer.split())[:90]}")
    except Exception as fout:
        print(f"  {label:<34} FAIL  {type(fout).__name__}: {str(fout)[:110]}")


LOTC = ("lotc", {})
LOTC_PLACEHOLDER = ("lotc", {"on_missing_component": "placeholder"})
ROOS_STRIKT = ("roos", {"strict_validation": True})
ROOS_LOSJES = ("roos", {"strict_validation": False})

print("MODEL A - een omgeving, eerst LOTC dan roos")
meet("alleen roos kent deze tag", ROOS_ONLY, [LOTC, ROOS_STRIKT])
meet("alleen LOTC kent deze tag", LOTC_ONLY, [LOTC, ROOS_STRIKT])
meet("beide kennen deze tag", BEIDE, [LOTC, ROOS_STRIKT])

print("MODEL B - een omgeving, eerst roos dan LOTC")
meet("alleen roos kent deze tag", ROOS_ONLY, [ROOS_STRIKT, LOTC])
meet("alleen LOTC kent deze tag", LOTC_ONLY, [ROOS_STRIKT, LOTC])
meet("beide kennen deze tag", BEIDE, [ROOS_STRIKT, LOTC])

print("MODEL C - twee losse omgevingen, een per systeem")
meet("roos-omgeving, roos-tag", ROOS_ONLY, [ROOS_STRIKT])
meet("roos-omgeving, LOTC-tag", LOTC_ONLY, [ROOS_STRIKT])
meet("LOTC-omgeving, LOTC-tag", LOTC_ONLY, [LOTC])
meet("LOTC-omgeving, roos-tag", ROOS_ONLY, [LOTC])

# Bestaat er een doorlaatstand, zodat de twee voorbewerkers elkaars tags met rust laten en
# in een keten kunnen staan? Dit is de enige manier waarop een enkele omgeving zou kunnen.
GEMENGD = f'<c-stack gap="md">{ROOS_ONLY}</c-stack>'
print("KETEN - laat een van de twee vreemde tags met rust?")
meet("LOTC(placeholder) -> roos(strikt)", GEMENGD, [LOTC_PLACEHOLDER, ROOS_STRIKT])
meet("LOTC(placeholder) -> roos(losjes)", GEMENGD, [LOTC_PLACEHOLDER, ROOS_LOSJES])
meet("roos(losjes) -> LOTC", GEMENGD, [ROOS_LOSJES, LOTC])
meet("roos(losjes) -> LOTC(placeholder)", GEMENGD, [ROOS_LOSJES, LOTC_PLACEHOLDER])
