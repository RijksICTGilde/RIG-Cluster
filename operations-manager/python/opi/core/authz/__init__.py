"""The authorization system: who may do what, for the API and the portal.

:mod:`opi.core.authz.actions` is the catalogue of actions, :mod:`opi.core.authz.engine`
the decision function, :mod:`opi.core.authz.rules` the rule register and
:mod:`opi.core.authz.matrix` the matrix that follows from that register.
"""

# The register has to be loaded before anything decides: an action without a rule is an error
# at startup, not a refusal at call time.
from opi.core.authz import rules as _rules  # noqa: F401  # imported for the registration it does
