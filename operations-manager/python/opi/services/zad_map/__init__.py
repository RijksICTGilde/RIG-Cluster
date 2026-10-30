"""De kaart van ZAD: de graafbouwers achter ``<zad-map>``.

``project_graph`` tekent een project, ``platform_graph`` het platform van dit cluster.
Het contract dat ze opleveren staat in :mod:`opi.services.zad_map.contract`.
"""

from opi.services.zad_map.contract import Graph
from opi.services.zad_map.platform import platform_graph
from opi.services.zad_map.project import incoming_links, project_graph

__all__ = ["Graph", "incoming_links", "platform_graph", "project_graph"]
