#!/usr/bin/env python3
"""Een nagebootste binary: schrijft zijn aanroep weg en doet verder niets.

``kubectl``, ``sops``, ``git``, ``mc``, ``kopia`` en ``skopeo`` in deze map zijn symlinks
hiernaartoe. In de PATH van de proefsandbox staan ze voor de echte, zodat de proef kan lezen
wat er werkelijk op een argumentenlijst terechtkomt zonder een cluster of een repo te raken.

De regel per aanroep gaat naar het bestand in ``ZAD_PROEF_AANROEPLOG``; zonder die variabele
wordt er niets geschreven en gedraagt de shim zich als een binary die niets kan.
"""

import json
import os
import sys
import time
from pathlib import Path


def main() -> int:
    log = os.environ.get("ZAD_PROEF_AANROEPLOG")
    if log:
        regel = {
            "tijd": time.time(),
            "tool": Path(sys.argv[0]).name,
            "argv": sys.argv[1:],
            "cwd": os.getcwd(),
        }
        with open(log, "a") as bestand:
            bestand.write(json.dumps(regel) + "\n")
    # Niet-nul, want deze binary heeft niets gedaan: een aanroeper die de uitkomst leest
    # moet dat zien in plaats van een geslaagde actie aan te nemen.
    print(f"{Path(sys.argv[0]).name}: nagebootst voor de aanvalsproef, geen actie", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
