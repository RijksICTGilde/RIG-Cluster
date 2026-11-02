"""Het rechtensysteem: wie mag wat, voor API en portaal.

:mod:`opi.core.authz.actions` is de catalogus van handelingen, :mod:`opi.core.authz.engine`
de beslisfunctie, :mod:`opi.core.authz.rules` het regelregister en
:mod:`opi.core.authz.matrix` de matrix die uit dat register volgt.
"""

# Het register moet geladen zijn voordat er iets beslist wordt: een action zonder regel is
# een fout bij het opstarten, niet een weigering bij het aanroepen.
from opi.core.authz import rules as _rules  # noqa: F401
