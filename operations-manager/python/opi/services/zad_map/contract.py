"""Contract v1 of the map: the graph ``<zad-map>`` draws.

The element knows no ZAD concepts. It reads columns (left to right), groups, nodes and
edges, and draws those. All text is plain text; only ``html`` in an info section is markup,
and that field takes markdown from the repo and nothing else.

Every graph is validated against these models, so one the element cannot read fails here, at
build time, instead of in the browser.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import AliasChoices, BaseModel, Field

#: The shapes the element knows.
Shape = Literal["card", "pill", "stack", "hub", "frame", "external"]

#: Colors are NAMES, not hex: the element translates them to the Rijkshuisstijl.
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

#: What a click on a node does.
Click = Literal["focus", "expand", "info"]

#: A node that exists but does not run here; the element gives it a dashed border.
State = Literal["elders", "extern", "uit"]


class Glyph(StrEnum):
    """The line icons the element draws itself.

    NOT the NLDD icon set: these live as paths in ``zad-map/src/lib/icons.js`` and never pass
    through the ``nldd_icon`` filter. A closed set, so a name the element does not know fails
    here instead of rendering as nothing; ``tests/test_zad_map_element.py`` holds the two
    lists against each other.
    """

    HEALTH = "health"
    DISK = "disk"
    METRICS = "metrics"
    CLIP = "clip"
    GLOBE = "globe"
    BOX = "box"


class Column(BaseModel):
    """One column of the drawing; the order in the list is left to right."""

    id: str


class Group(BaseModel):
    """A frame with nodes in it.

    ``pack`` says who lays the contents out: ``elk`` leaves it to the layout, which is needed
    as soon as edges run into the group, while ``grid`` and ``row`` leave it to the element.
    ``band`` is the wide bar under the drawing.
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
    """One line on a card: a bar, or an icon with text.

    A bar shows ``req`` and ``lim`` on the scale of ``max``, with ``text`` underneath.
    ``href`` may only be a web address the map derived itself.
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
    """One block in the panel: a list, or rendered markdown from the repo."""

    heading: str = ""
    items: list[InfoItem] = Field(default_factory=list)
    html: str = ""


class Info(BaseModel):
    eyebrow: str = ""
    title: str
    subtitle: str = ""
    sections: list[InfoSection] = Field(default_factory=list)


class Node(BaseModel):
    """One node. ``column`` or ``group`` says where it belongs, never both."""

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
    #: Nodes that light up along with this one on focus, without an edge running to them.
    related: list[str] = Field(default_factory=list)
    children: list[Node] = Field(default_factory=list)
    info: Info | None = None


class Edge(BaseModel):
    """An edge between two node or group ids."""

    # Split aliases, because ``from`` is reserved in Python: the JSON field is ``from``, the
    # Python name ``from_``. A single ``alias`` would leave the constructor accepting only
    # ``from=``, which does not parse.
    from_: str = Field(serialization_alias="from", validation_alias=AliasChoices("from", "from_"))
    to: str
    color: Color | None = None
    label: str = ""


class Graph(BaseModel):
    version: Literal[1] = 1
    columns: list[Column] = Field(default_factory=list)
    groups: list[Group] = Field(default_factory=list)
    nodes: list[Node] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)

    def as_json(self) -> dict:
        """The graph as the element reads it: aliases, with empty fields left out."""
        return self.model_dump(by_alias=True, exclude_none=True)
