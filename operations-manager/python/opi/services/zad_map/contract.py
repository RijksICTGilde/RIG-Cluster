"""Contract v1 van de kaart: de graaf die ``<zad-map>`` tekent.

Het element kent geen ZAD-begrippen. Het leest kolommen (links naar rechts), groepen,
knopen en lijnen, en tekent die. Alle tekst is platte tekst; alleen ``html`` in een
infosectie is markup, en dat veld krijgt uitsluitend markdown uit de repo.

De modellen staan hier zodat elke uitvoer gevalideerd is: een graaf die het element niet
kan lezen valt hier om, bij het bouwen, en niet in de browser.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import AliasChoices, BaseModel, Field

#: De vormen die het element kent.
Shape = Literal["card", "pill", "stack", "hub", "frame", "external"]

#: Kleuren zijn NAMEN, geen hex: het element vertaalt ze naar de Rijkshuisstijl.
Color = Literal[
    "component",
    "deployment",
    "team",
    "project",
    "web",
    "identity",
    "data",
    "ops",
    "link",
    "gitops",
    "network",
    "platform",
    "external",
]

#: Wat een klik op een knoop doet.
Click = Literal["focus", "expand", "info"]

#: Een knoop die er wel is maar hier niet draait; het element geeft hem een stippelrand.
State = Literal["elders", "extern", "uit"]


class Glyph(StrEnum):
    """De lijnicoontjes die het element zelf tekent.

    Dit is NIET de NLDD-iconenset: ze staan als pad in ``zad-map/src/lib/icons.js`` en
    komen nooit door het ``nldd_icon``-filter. Een gesloten verzameling, zodat een naam
    die het element niet kent hier omvalt in plaats van als niets te renderen;
    ``tests/test_zad_map_element.py`` legt de twee lijsten naast elkaar.
    """

    HEALTH = "health"
    DISK = "disk"
    METRICS = "metrics"
    CLIP = "clip"
    GLOBE = "globe"
    BOX = "box"


class Column(BaseModel):
    """Een kolom in de plaat; de volgorde in de lijst is links naar rechts."""

    id: str


class Group(BaseModel):
    """Een kader met knopen erin.

    ``pack`` zegt wie de inhoud neerlegt: ``elk`` laat de layout het doen (nodig zodra er
    lijnen naar binnen lopen), ``grid`` en ``row`` laat het element zelf doen.
    ``band`` is de brede balk onder de plaat.
    """

    id: str
    title: str
    eyebrow: str = ""
    subtitle: str = ""
    column: str | None = None
    pack: Literal["grid", "row", "elk"] = "elk"
    band: bool = False
    style: Literal["", "external"] = ""


class Row(BaseModel):
    """Een regel op een kaart: een staafje, of een icoon met tekst.

    Een staafje toont ``req`` en ``lim`` op de schaal ``max``; ``text`` is wat eronder
    staat. ``href`` mag alleen een webadres zijn dat de kaart zelf heeft afgeleid.
    """

    bar: Literal["mem", "cpu"] | None = None
    req: float | None = None
    lim: float | None = None
    max: float | None = None
    icon: Glyph | None = None
    text: str
    href: str | None = None


class InfoItem(BaseModel):
    text: str
    mono: bool = False
    href: str | None = None


class InfoSection(BaseModel):
    """Een blok in het paneel: een lijst, of gerenderde markdown uit de repo."""

    heading: str = ""
    items: list[InfoItem] = Field(default_factory=list)
    html: str = ""


class Info(BaseModel):
    eyebrow: str = ""
    title: str
    subtitle: str = ""
    sections: list[InfoSection] = Field(default_factory=list)


class Node(BaseModel):
    """Een knoop. ``column`` of ``group`` zegt waar hij hoort, nooit allebei."""

    id: str
    column: str | None = None
    group: str | None = None
    shape: Shape = "card"
    color: Color | None = None
    eyebrow: str = ""
    title: str = ""
    subtitle: str = ""
    rows: list[Row] = Field(default_factory=list)
    state: State | None = None
    click: Click | None = None
    #: Knopen die mee oplichten bij focus, zonder dat er een lijn naartoe loopt.
    related: list[str] = Field(default_factory=list)
    children: list[Node] = Field(default_factory=list)
    info: Info | None = None


class Edge(BaseModel):
    """Een lijn tussen twee knoop- of groep-ids.

    ``from`` is een gereserveerd woord in Python, vandaar de alias.
    """

    # Gesplitste aliassen: de JSON heet ``from``, de Python-naam ``from_``. Een enkele
    # ``alias`` zou de constructor alleen nog ``from=`` laten accepteren, en dat is geen
    # geldige Python.
    from_: str = Field(serialization_alias="from", validation_alias=AliasChoices("from", "from_"))
    to: str
    color: Color | None = None
    label: str = ""
    #: ``dashed`` for a line that is a rule rather than a fixed connection.
    style: Literal["", "dashed"] = ""


class Graph(BaseModel):
    version: Literal[1] = 1
    columns: list[Column] = Field(default_factory=list)
    groups: list[Group] = Field(default_factory=list)
    nodes: list[Node] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)

    def as_json(self) -> dict:
        """De graaf zoals het element hem leest: aliassen, zonder lege velden."""
        return self.model_dump(by_alias=True, exclude_none=True)
