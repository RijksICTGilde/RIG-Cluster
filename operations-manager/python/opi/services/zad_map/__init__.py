"""The ZAD map: the graph builders behind ``<zad-map>``.

``project_graph`` draws one project, ``platform_graph`` this cluster's platform. The
contract they produce is in :mod:`opi.services.zad_map.contract`.
"""

from opi.services.zad_map.contract import Graph
from opi.services.zad_map.platform import platform_graph
from opi.services.zad_map.project import incoming_links, project_graph

__all__ = ["Graph", "incoming_links", "platform_graph", "project_graph"]
