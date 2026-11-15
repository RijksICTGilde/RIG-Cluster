"""Environment defaults every pytest run in this project needs before the first opi import.

They sit in the ROOT conftest and not in ``tests/conftest.py`` because pytest imports the
conftest of each collected directory, in the order the directories are named. A run that
collects ``security_tests`` beside ``tests`` imports its conftest first, and that one
imports ``opi.core.config``, which builds ``settings`` from the environment once. Setting
these defaults later no longer changes anything.
"""

import os

# setup_components reads LOTC_STRICT while importing opi.http.render.templates_lotc, so
# setting it after that import does nothing. Strict refuses an unknown attribute VALUE
# instead of rendering it silently, which is what the tests and CI want and the production
# image does not.
os.environ.setdefault("LOTC_STRICT", "1")

# Without a setting ENVIRONMENT falls back to "production", and then create_app leaves the
# proof and test routes unregistered. The tests measure those routes, so: "local".
os.environ.setdefault("ENVIRONMENT", "local")
