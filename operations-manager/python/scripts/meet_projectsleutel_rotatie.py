"""De rondgang van RC-289 op een cluster: roteer de projectsleutel van een project met een
database en een PVC, en kijk of de back-ups daarna nog werken.

Waarom een script en geen test: dit is geen poort maar een MEETBANK. Het maakt een echt
project aan, vult database en PVC, maakt een back-up, roteert de sleutel en zet daarna de
snapshot van voor EN na de rotatie terug. De opbrengst is de uitvoer; het script beweert zelf
niets over de uitkomst, het laat zien wat er gebeurde. Waarden komen er als lengte en
vingerafdruk uit, nooit als tekst.

    eval "$(sandbox-gegevens --env)"
    export ZAD_TOKEN=<een SSO-token van een platformbeheerder>
    export ZAD_OTP=<een verse code uit de authenticator van die beheerder>
    uv run python scripts/meet_projectsleutel_rotatie.py alles

De fase ``roteer`` vraagt sinds RC-377 een verse code naast het token; een code geldt maar
een keer, dus draai die fase los met een code die op dat moment in de authenticator staat.

Losse fasen kunnen ook, in deze volgorde, met de toestand in --toestand:

    maak sync vul backup1 oude-sleutel roteer probe vul2 backup2 restore1 controleer1
    restore2 controleer2 recipients uitslagen op

Twee dingen die deze rondgang niet van ArgoCD laat afhangen, omdat de sandbox-ArgoCD geen
tenant-workloads synct (zie de opmerking op PR 265): de PVC komt uit het gegenereerde
manifest in `zad-deployments` in plaats van uit een sync, en in de PVC wordt geschreven met
een busybox-pod in plaats van met de workload van het project. De fasen wachten op het
RESULTAAT van een stap (projectbestand, namespace-Secret, nieuwe sleutel) en niet op de
einduitslag van de taak: elke verwerking eindigt hier pas na het ArgoCD-budget van 600s.

Wat de rondgang moet aantonen:

1. ``kopia repository connect`` met het OUDE afgeleide wachtwoord faalt na de rotatie
   ("invalid repository password"), met het NIEUWE slaagt hij en ziet hij de snapshots;
2. de back-up NA de rotatie slaagt, en draait via de normale backup-pod in de namespace;
3. de snapshot van VOOR de rotatie en die van NA de rotatie zijn beide terug te zetten, met
   de juiste inhoud in database en PVC (3 rijen / "voor-rotatie", 5 rijen / "na-rotatie").

De wizardstappen worden zonder browser herhaald: een browserronde duurt op de sandbox
minuten en de hele rondgang moet in een hold van 30 minuten passen. De inhoud van de stappen
komt uit een opname van een echte browserronde (de stappen in ``_WIZARDSTAPPEN``); wijzigt de
wizard, dan wijzigt die opname mee.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from typing import Any

import httpx
import yaml
from itsdangerous import TimestampSigner

FLOW = "create-project"
HX = {"hx-request": "true", "content-type": "application/json"}


def log(*a: Any) -> None:
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def vingerafdruk(waarde: str) -> str:
    """Een waarde herkenbaar maken zonder hem te tonen."""
    return f"sha256:{hashlib.sha256(waarde.encode()).hexdigest()[:12]} ({len(waarde)} tekens)"


class Meting:
    """De rondgang, met zijn toestand in een bestand zodat fasen los te draaien zijn."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.base = args.base_url.rstrip("/")
        self.secret = args.secret_key
        self.zad_token = args.zad_token
        self.zad_otp = args.zad_otp
        self.cluster = args.cluster
        self.opi_namespace = args.opi_namespace
        self.context = args.context
        self.image = args.image
        self.s3_endpoint = args.s3_endpoint
        self.toestand_pad = args.toestand
        self.forgejo_url = args.forgejo_url.rstrip("/")
        self.forgejo_user = args.forgejo_user
        self.forgejo_password = args.forgejo_password
        self.user = {"sub": "meting-rc289", "email": args.email, "name": "RC-289 meting"}

    # ---------------------------------------------------------------- toestand

    def lees(self) -> dict:
        if os.path.exists(self.toestand_pad):
            with open(self.toestand_pad) as fh:
                return json.load(fh)
        return {}

    def schrijf(self, **kv: Any) -> dict:
        # 0600: de toestand draagt de api-key van het project en de afgeleide kopia-wachtwoorden.
        # De chmod erbij, want de mode van os.open geldt alleen voor een nieuw bestand.
        s = self.lees() | kv
        with open(os.open(self.toestand_pad, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as fh:
            json.dump(s, fh, indent=1)
        os.chmod(self.toestand_pad, 0o600)
        return s

    # ------------------------------------------------------------------ gereed

    def sessiecookie(self) -> str:
        # login_at: zonder inlogtijdstip weigert session_guard de sessie.
        payload = base64.b64encode(json.dumps({"user": {**self.user, "login_at": time.time()}}).encode()).decode()
        return TimestampSigner(self.secret).sign(payload).decode()

    #: Een hik van de API-server van dit cluster, geen uitkomst van de meting.
    _HIK = ("current server API group list", "doesn't have a resource type", "connection refused", "i/o timeout")

    def _draai(self, cmd: list[str], *, timeout: int, stdin: str | None) -> subprocess.CompletedProcess[str]:
        """Draai het commando, en nog een keer als de API-server hikte (die is hier traag)."""
        for poging in (1, 2):
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=stdin)  # noqa: S603 - vaste argv, geen shell
            if r.returncode == 0 or poging == 2 or not any(h in r.stderr for h in self._HIK):
                return r
            log(f"  API-server hikte, nog een poging: {r.stderr.strip().splitlines()[-1][:120]}")
            time.sleep(5)
        raise AssertionError("onbereikbaar")

    def kubectl(self, *args: str, check: bool = True, timeout: int = 120, stdin: str | None = None) -> str:
        cmd = ["kubectl", f"--context={self.context}", "--request-timeout=60s", *args]
        r = self._draai(cmd, timeout=timeout, stdin=stdin)
        if check and r.returncode != 0:
            raise RuntimeError(f"kubectl {' '.join(args)[:120]} -> {r.returncode}: {r.stderr[:600]}")
        return r.stdout

    def in_opi(self, script: str, *, check: bool = True, timeout: int = 300) -> tuple[int, str]:
        """Draai een shellscript in de OPI-pod: daar staan kopia, age en sops."""
        cmd = [
            "kubectl",
            f"--context={self.context}",
            "--request-timeout=60s",
            "-n",
            self.opi_namespace,
            "exec",
            "deploy/operations-manager",
            "--",
            "sh",
            "-c",
            script,
        ]
        r = self._draai(cmd, timeout=timeout, stdin=None)
        uit = r.stdout + r.stderr
        if check and r.returncode != 0:
            raise RuntimeError(f"in de OPI-pod mislukt ({r.returncode}): {uit[-800:]}")
        return r.returncode, uit

    def api(
        self,
        method: str,
        pad: str,
        key: str | None = None,
        bearer: str | None = None,
        otp: str | None = None,
        **kw: Any,
    ) -> httpx.Response:
        """``key`` is een projectsleutel, ``bearer`` het token van een platformbeheerder.

        ``otp`` is de verse code die een wijzigende beheerroute daarnaast vraagt (RC-377).
        """
        headers = {"Content-Type": "application/json"}
        if key:
            headers["X-API-Key"] = key
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        if otp:
            headers["X-ZAD-OTP"] = otp
        with httpx.Client(verify=False, timeout=1800.0) as c:  # noqa: S501 - zelfgetekend sandboxcertificaat
            return c.request(method, f"{self.base}{pad}", headers=headers, **kw)

    def geheim(self, namespace: str, naam: str) -> dict[str, str]:
        ruw = self.kubectl("get", "secret", naam, "-n", namespace, "-o", "json")
        return {k: base64.b64decode(v).decode() for k, v in json.loads(ruw)["data"].items()}

    def sops_age_key(self, namespace: str) -> str:
        ruw = self.kubectl("get", "secret", "sops-age-key", "-n", namespace, "-o", "jsonpath={.data.key}")
        return base64.b64decode(ruw.strip()).decode()

    def projectbestand(self, naam: str) -> dict:
        return yaml.safe_load(self.in_opi(f"cat /data/zad-projects-warm/projects/{naam}.yaml")[1])

    def bestaat_pvc(self, namespace: str, naam: str) -> bool:
        return self.kubectl("get", "pvc", naam, "-n", namespace, check=False) != ""

    def psql(self, secret: dict[str, str], statement: str, database: str | None = None) -> tuple[int, str]:
        """SQL tegen de database van de deployment, met de inloggegevens die hij zelf gebruikt."""
        cmd = [
            "kubectl",
            f"--context={self.context}",
            "--request-timeout=60s",
            "run",
            f"meet-psql-{os.urandom(4).hex()}",
            "-n",
            self.opi_namespace,
            "--rm",
            "-i",
            "--restart=Never",
            "--image=postgres:16-alpine",
            "--env",
            f"PGPASSWORD={secret['DATABASE_PASSWORD']}",
            "--command",
            "--",
            "psql",
            "-h",
            secret["DATABASE_SERVER_HOST"],
            "-U",
            secret["DATABASE_SERVER_USER"],
            "-d",
            database or secret["DATABASE_DB"],
            "-v",
            "ON_ERROR_STOP=1",
            "-tAc",
            statement,
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)  # noqa: S603 - vaste argv, geen shell
        return r.returncode, (r.stdout + r.stderr).strip()

    @staticmethod
    def derive_backup_password(namespace: str, age_key: str) -> str:
        """Dezelfde afleiding als ``opi/manager/backup/base.py``."""
        material = f"kopia-backup-{namespace}-{age_key}".encode()
        return base64.b64encode(hashlib.sha256(material).digest()).decode()[:32]

    # ------------------------------------------------------------ de wizard

    def _wizardstappen(self, display_name: str) -> list[tuple[str, dict]]:
        """De stapposts van de create-wizard voor een project met een database en een PVC."""
        return [
            (
                "identity",
                {
                    "display-name": display_name,
                    "description": f"RC-289 meting {display_name}",
                    "clusters": self.cluster,
                },
            ),
            ("services", {"services": ["persistent-storage", "postgresql-database"]}),
            (
                "postgresql-schemas-config",
                {
                    "_services-config": {"postgresql-database": {"config": {"connection-limit": ""}}},
                    "_gerenderde-reeksen": "_services-config/postgresql-database/config/schemas",
                },
            ),
            ("team", {"_gerenderde-reeksen": "users", "users": [{"email": self.user["email"], "role": "admin"}]}),
            (
                "components",
                {
                    "_gerenderde-reeksen": [
                        "components",
                        "components[0]/ports/inbound",
                        "components[0]/path",
                        "components[0]/_services-config{persistent-storage}/config",
                    ],
                    "components": [
                        {
                            "name": "web",
                            "image": self.image,
                            "command": "",
                            "resources": {"requests": {"memory": "64Mi"}, "limits": {"memory": "256Mi"}},
                            "ports": {"inbound": ["8080"]},
                            "services": ["persistent-storage", "postgresql-database"],
                            "path": [{"match": "/", "rewrite": ""}],
                            "aliases": "",
                            "user-env-vars": "",
                            "_services-config": [
                                {
                                    "persistent-storage": {
                                        "config": [{"name": "data", "size": "100Mi", "mount-path": "/data"}]
                                    }
                                }
                            ],
                        }
                    ],
                },
            ),
            ("deployment", {"deployments": [{"name": "productie"}]}),
            (
                "domains",
                {
                    "deployments": [
                        {
                            "_services-config": [
                                {
                                    "publish-on-web": {
                                        "config": {"base-domain": "", "domain-format": "component-deployment-project"}
                                    }
                                }
                            ]
                        }
                    ]
                },
            ),
        ]

    def _neem_koekjes_over(self, client: httpx.Client, response: httpx.Response) -> None:
        """De app zet zijn sessiekoekje als ``Secure``; httpx bewaart dat niet over http.

        Dan draagt elk verzoek nog het beginkoekje, raakt de wizardstand kwijt en antwoordt
        elke stap met een 303 naar het begin: na volgen een 200, dus stil goed.
        """
        for eerder in response.history:
            self._neem_koekjes_over(client, eerder)
        for kop in response.headers.get_list("set-cookie"):
            naam, _, rest = kop.partition("=")
            client.cookies.set(naam.strip(), rest.split(";")[0].strip())

    def _csrf(self, client: httpx.Client) -> dict[str, str]:
        """De dubbel-verzonden CSRF-token plus de herkomst die de middleware eist."""
        # Niet cookies.get(): na het volgen van de redirects staat hetzelfde koekje er voor
        # meer dan een domein, en dan werpt httpx CookieConflict. De laatste is de geldige.
        token = next((k.value or "" for k in reversed(list(client.cookies.jar)) if k.name == "csrf_token"), "")
        if not token:
            raise RuntimeError("geen csrf_token-koekje na het openen van de wizard")
        return {"x-csrf-token": token, "origin": self.base, "referer": f"{self.base}/forms/wizard/{FLOW}"}

    def herhaal_de_wizard(self, display_name: str) -> str:
        """Loop de wizard af zonder browser; geef het task_id van de aanmaaktaak terug."""
        with httpx.Client(base_url=self.base, verify=False, timeout=120.0, follow_redirects=False) as c:  # noqa: S501 - zelfgetekend sandboxcertificaat
            c.cookies.set("session", self.sessiecookie())
            for pad in ("/forms/wizard/restart", "/forms/wizard/start", f"/forms/wizard/{FLOW}"):
                r = c.get(pad, follow_redirects=True)
                r.raise_for_status()
                self._neem_koekjes_over(c, r)
            for sectie, body in self._wizardstappen(display_name):
                r = c.post(f"/forms/wizard/{FLOW}/step/{sectie}", json=body, headers=dict(HX) | self._csrf(c))
                r.raise_for_status()
                self._neem_koekjes_over(c, r)
                if "field-error" in r.text or "veld is verplicht" in r.text:
                    raise RuntimeError(f"stap {sectie} afgekeurd: {r.text[:800]}")
                log(f"  stap {sectie}: {r.status_code}")
            r = c.post(f"/forms/wizard/{FLOW}/submit", headers={"hx-request": "true"} | self._csrf(c))
            if r.status_code >= 400:
                raise RuntimeError(f"submit gaf {r.status_code}: {r.text[:400]}")
            treffer = re.search(r"/projects/progress/([0-9a-fA-F-]+)", r.headers.get("HX-Redirect", ""))
            if not treffer:
                raise RuntimeError(f"submit gaf geen voortgangspagina: {r.text[:400]}")
            return treffer.group(1)

    def wacht_op_aanmaak(self, task_id: str, *, timeout: float = 1800.0) -> tuple[str, str]:
        """Volg de aanmaaktaak op de voortgangspagina; geef (status, projectnaam).

        Niet via ``/api/tasks``: die route wil de projectsleutel, en juist bij het AANMAKEN
        werkt die nog niet. De voortgangspagina hangt aan de sessie, net als in de browser.
        """
        deadline = time.monotonic() + timeout
        with httpx.Client(base_url=self.base, verify=False, timeout=60.0) as c:  # noqa: S501 - zelfgetekend sandboxcertificaat
            c.cookies.set("session", self.sessiecookie())
            laatst = ""
            while time.monotonic() < deadline:
                r = c.get(f"/projects/progress/{task_id}/fragment")
                r.raise_for_status()
                treffer = re.search(r"/projects/([^/'\"]+)/details", r.text)
                naam = treffer.group(1) if treffer else ""
                if 'color="critical"' in r.text:
                    status = "failed"
                elif 'color="success"' in r.text:
                    status = "completed"
                else:
                    status = "running"
                if status != laatst:
                    log(f"  aanmaaktaak {task_id}: {status} {naam}")
                    laatst = status
                if status in ("completed", "failed"):
                    return status, naam
                time.sleep(5)
        raise TimeoutError(f"aanmaaktaak {task_id} is na {timeout}s nog {laatst!r}")

    def wacht_op_taak(self, task_id: str, api_key: str, *, timeout: float = 1800.0) -> dict:
        deadline = time.monotonic() + timeout
        with httpx.Client(base_url=self.base, verify=False, timeout=60.0) as c:  # noqa: S501 - zelfgetekend sandboxcertificaat
            laatst = ""
            while time.monotonic() < deadline:
                r = c.get(f"/api/tasks/{task_id}", headers={"X-API-Key": api_key})
                r.raise_for_status()
                body = r.json()
                status = body.get("status", "")
                if status != laatst:
                    log(f"  taak {task_id}: {status}")
                    laatst = status
                if status in ("completed", "failed", "cancelled"):
                    return body
                time.sleep(5)
        raise TimeoutError(f"taak {task_id} is na {timeout}s nog {laatst!r}")

    def api_key_uit_projectbestand(self, project: str) -> str:
        """De API-key uit het projectbestand, ontsleuteld met OPI's eigen AGE-helpers.

        Niet van de detailpagina: die hangt aan de opmaak van een template, en dit hangt aan
        de code die de waarde ook schrijft.
        """
        return self._uit_opi(
            "import asyncio, yaml; "
            "from opi.utils.age import decrypt_age_content, get_decoded_project_private_key; "
            f"d=yaml.safe_load(open('/data/zad-projects-warm/projects/{project}.yaml')); "
            "k=asyncio.run(get_decoded_project_private_key(d)); "
            "print('MEET:' + asyncio.run(decrypt_age_content(d['config']['api-key'], k)))",
            1,
        )[0]

    # -------------------------------------------------------------------- fasen

    def wacht_op_bewijs(self, wat: str, probe: Any, *, timeout: float, interval: float = 5.0) -> Any:
        """Wacht tot een stap zijn resultaat heeft neergezet, niet tot de taak eindigt.

        Op deze sandbox eindigt elke verwerking pas na het ArgoCD-budget van 600s, en wat
        de rondgang nodig heeft staat er lang daarvoor. Zie de opmerking op PR 265.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            uitkomst = probe()
            if uitkomst:
                log(f"  bewijs voor {wat} na {timeout - (deadline - time.monotonic()):.0f}s")
                return uitkomst
            time.sleep(interval)
        raise TimeoutError(f"geen bewijs voor {wat} binnen {timeout:.0f}s")

    def projectnaam(self, task_id: str, *, timeout: float = 180.0) -> str:
        """De naam die het aanmaken aan het project gaf, van de voortgangspagina.

        Niet de weergavenaam: die krijgt een eigen achtervoegsel, dus de map in
        zad-deployments en de namespace heten anders dan wat de wizard meekreeg.
        """
        with httpx.Client(base_url=self.base, verify=False, timeout=60.0) as c:  # noqa: S501 - zelfgetekend sandboxcertificaat
            c.cookies.set("session", self.sessiecookie())

            def naam() -> str:
                r = c.get(f"/projects/progress/{task_id}/fragment")
                r.raise_for_status()
                treffer = re.search(r"/projects/([^/'\"]+)/details", r.text)
                return treffer.group(1) if treffer else ""

            return self.wacht_op_bewijs(f"projectnaam van taak {task_id}", naam, timeout=timeout)

    def maak(self) -> None:
        weergavenaam = f"rc289-{os.urandom(3).hex()}"
        log(f"wizard herhalen voor '{weergavenaam}'")
        task_id = self.herhaal_de_wizard(weergavenaam)
        log(f"aanmaaktaak {task_id}")
        naam = self.projectnaam(task_id)
        log(f"projectnaam: {naam}")

        def bestand() -> dict | None:
            code, uit = self.in_opi(f"cat /data/zad-projects-warm/projects/{naam}.yaml", check=False)
            if code != 0:
                return None
            return yaml.safe_load(uit)

        project_data = self.wacht_op_bewijs(f"projectbestand van {naam}", bestand, timeout=300.0)
        deployment = project_data["deployments"][0]["name"]
        ns = f"rig-{naam}"
        self.wacht_op_bewijs(
            f"sops-age-key in {ns}",
            lambda: self.kubectl("get", "secret", "sops-age-key", "-n", ns, check=False),
            timeout=300.0,
        )
        # Het database-Secret, de PVC en de ServiceAccount landen in zad-deployments en niet
        # in de namespace: die komen van ArgoCD, en die synct hier niet (zie sync()).
        self.wacht_op_bewijs(
            f"database-Secret in zad-deployments voor {naam}/{deployment}",
            lambda: self._bestaat_in_zad_deployments(
                f"{self.cluster}/{naam}/{deployment}/{deployment}-database-secret.sops.yaml"
            ),
            timeout=600.0,
        )
        key = self.api_key_uit_projectbestand(naam)
        self.schrijf(project=naam, deployment=deployment, namespace=ns, api_key=key, aanmaaktaak=task_id)
        log(f"project={naam} deployment={deployment} sleutel={vingerafdruk(key)}")

    def _bestaat_in_zad_deployments(self, pad: str) -> bool:
        try:
            self._uit_zad_deployments(pad)
        except RuntimeError:
            return False
        return True

    def _uit_zad_deployments(self, pad: str) -> str:
        """Lees een gegenereerd manifest uit de zad-deployments-repo op Forgejo.

        Niet uit de clone in de OPI-pod: die is tijdelijk en kan tussen twee fasen weg zijn.
        """
        with httpx.Client(verify=False, timeout=60.0, auth=(self.forgejo_user, self.forgejo_password)) as c:  # noqa: S501 - zelfgetekend sandboxcertificaat
            r = c.get(f"{self.forgejo_url}/api/v1/repos/rig-admin/zad-deployments/raw/{pad}")
        if r.status_code != 200:
            raise RuntimeError(f"{pad} niet in zad-deployments: {r.status_code} {r.text[:200]}")
        return r.text

    def sync(self) -> None:
        """Zet de resources neer die de rondgang nodig heeft en die ArgoCD hier niet synct.

        De tenant-resources van een deployment komen van ArgoCD, en `user-applications` staat
        op deze sandbox op SyncError. Ze komen daarom uit het gegenereerde manifest in
        `zad-deployments` zelf en niet uit een eigen spec: de ServiceAccount van het project
        (de backup-pod draait eronder), de PVC, en het database-Secret (met de projectsleutel
        versleuteld, want de database-back-up leest de inloggegevens daaruit).
        """
        s = self.lees()
        p, d = s["project"], s["deployment"]

        sa = self._uit_zad_deployments(f"{self.cluster}/{p}/_project/platform-{p}-sa.yaml")
        log(self.kubectl("apply", "-f", "-", stdin=sa).strip())

        manifest = self._uit_zad_deployments(f"{self.cluster}/{p}/{d}/web-data-pvc.yaml")
        pvcs = re.findall(r'^\s*name:\s*"?([a-z0-9-]+)"?', manifest, re.MULTILINE)
        log(f"PVC uit zad-deployments: {pvcs}")
        log(self.kubectl("apply", "-f", "-", stdin=manifest).strip())

        versleuteld = self._uit_zad_deployments(f"{self.cluster}/{p}/{d}/{d}-database-secret.sops.yaml")
        ontsleuteld = subprocess.run(
            # sops van het PATH, net als kubectl hierboven
            ["sops", "-d", "--input-type", "yaml", "--output-type", "yaml", "/dev/stdin"],  # noqa: S607
            input=versleuteld,
            capture_output=True,
            text=True,
            timeout=120,
            env=os.environ | {"SOPS_AGE_KEY": self.sops_age_key(s["namespace"])},
        )
        if ontsleuteld.returncode != 0:
            raise RuntimeError(f"sops -d op het database-Secret mislukt: {ontsleuteld.stderr[-400:]}")
        log(self.kubectl("apply", "-f", "-", stdin=ontsleuteld.stdout).strip())
        self.schrijf(pvcs=pvcs, handmatig_gesynct="ServiceAccount, PVC en database-Secret uit zad-deployments")

    def in_pvc(self, namespace: str, pvc: str, script: str, *, timeout: int = 420) -> str:
        """Draai een busybox-pod met de PVC gemount, om erin te schrijven of eruit te lezen.

        Niet in de workload van het project: die komt van ArgoCD en synct hier niet. En niet
        met `kubectl run -i`: die attach gaf onder clusterlast lege uitvoer terwijl de pod
        gewoon liep, dus de pod schrijft naar zijn log en die lezen we terug.
        """
        naam = f"meet-pvc-{os.urandom(3).hex()}"
        manifest = json.dumps(
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {"name": naam, "namespace": namespace},
                "spec": {
                    "restartPolicy": "Never",
                    "containers": [
                        {
                            "name": "pvc",
                            "image": "busybox:1.36",
                            "command": ["sh", "-c", script],
                            "volumeMounts": [{"name": "v", "mountPath": "/mnt"}],
                        }
                    ],
                    "volumes": [{"name": "v", "persistentVolumeClaim": {"claimName": pvc}}],
                },
            }
        )
        self.kubectl("apply", "-f", "-", stdin=manifest)
        try:
            self.kubectl(
                "wait",
                "--for=jsonpath={.status.phase}=Succeeded",
                f"pod/{naam}",
                "-n",
                namespace,
                f"--timeout={timeout}s",
            )
            return self.kubectl("logs", naam, "-n", namespace)
        finally:
            self.kubectl("delete", "pod", naam, "-n", namespace, "--wait=false", check=False)

    def vul(self) -> None:
        s = self.lees()
        ns = s["namespace"]
        db = self.geheim(ns, f"{s['deployment']}-database")
        schema = db["DATABASE_SCHEMA"]
        # S608: de schemanaam komt uit het geheim van de deployment, niet van een gebruiker.
        code, uit = self.psql(
            db,
            f'CREATE TABLE IF NOT EXISTS "{schema}".voor (id int); '  # noqa: S608
            f'INSERT INTO "{schema}".voor VALUES (1),(2),(3);',
        )
        log(f"database vullen: {code} {uit.splitlines()[:1]}")
        if code != 0:
            raise RuntimeError(uit[:800])
        pvc = s["pvcs"][0]
        self.in_pvc(ns, pvc, "echo voor-rotatie > /mnt/rc289.txt && cat /mnt/rc289.txt")
        log(f"PVC {pvc} gevuld: rc289.txt = voor-rotatie")
        self.schrijf(db_voor_rotatie=db["DATABASE_DB"], schema=schema, rijen_voor=3, bestand_voor="voor-rotatie")

    def backup(self, label: str) -> None:
        s = self.lees()
        p, d, key = s["project"], s["deployment"], s["api_key"]
        log(f"backup ({label}) van {p}/{d}")
        r = self.api(
            "POST", f"/api/v1/backup/project/{p}/deployment/{d}", key, json={"resource_types": ["database", "pvc"]}
        )
        log(f"  POST backup -> {r.status_code}")
        if r.status_code != 200:
            raise RuntimeError(r.text[:1000])
        body = r.json()
        log(f"  status={body.get('status')}")
        runs = self.api("GET", f"/api/v1/backup/runs/{p}/{d}", key)
        runs.raise_for_status()
        lijst = sorted(runs.json()["runs"], key=lambda x: x.get("timestamp", ""))
        log(f"  backup-run {lijst[-1]['backup_run_id']} (nu {len(lijst)} runs)")
        self.schrijf(**{f"run_{label}": lijst[-1]["backup_run_id"], f"backup_{label}": body.get("status")})

    def oude_sleutel(self) -> None:
        s = self.lees()
        ns = s["namespace"]
        age_key = self.sops_age_key(ns)
        pw = self.derive_backup_password(ns, age_key)
        pub = self.projectbestand(s["project"])["config"]["age-public-key"]
        log(f"oude projectsleutel (publiek): {pub}")
        log(f"oude age-sleutel in {ns}: {vingerafdruk(age_key)}")
        log(f"oud afgeleid kopia-wachtwoord: {vingerafdruk(pw)}")
        self.schrijf(oude_public_key=pub, oud_kopia=pw, oude_age_key_fp=vingerafdruk(age_key))

    def roteer(self) -> None:
        s = self.lees()
        p, ns = s["project"], s["namespace"]
        r = self.api("POST", f"/api/v2/projects/{p}/:rotate-project-key", bearer=self.zad_token, otp=self.zad_otp)
        log(f"POST :rotate-project-key -> {r.status_code}")
        if r.status_code not in (200, 202):
            raise RuntimeError(r.text[:1000])
        task_id = r.json()["task_id"]

        # De taak eindigt pas na het ArgoCD-budget; het nieuwe paar staat er lang daarvoor.
        def nieuwe_sleutel_in_namespace() -> str:
            inhoud = self.sops_age_key(ns)
            treffer = re.search(r"public key:\s*(age1[a-z0-9]+)", inhoud)
            pub = treffer.group(1) if treffer else ""
            return pub if pub and pub != s["oude_public_key"] else ""

        pub_uit_secret = self.wacht_op_bewijs(
            f"nieuwe sops-age-key in {ns}", nieuwe_sleutel_in_namespace, timeout=420.0
        )
        nieuw_pub = self.projectbestand(p)["config"]["age-public-key"]
        if nieuw_pub != pub_uit_secret:
            log(f"LET OP: projectbestand noemt {nieuw_pub}, het namespace-Secret {pub_uit_secret}")
        nieuwe_age = self.sops_age_key(ns)
        res: dict[str, Any] = {"taak": "loopt nog (ArgoCD-budget)", "task_id": task_id}
        nieuw_kopia = self.derive_backup_password(ns, nieuwe_age)
        log(f"nieuwe projectsleutel (publiek): {nieuw_pub}")
        log(f"nieuwe age-sleutel in {ns}: {vingerafdruk(nieuwe_age)}")
        log(f"nieuw afgeleid kopia-wachtwoord: {vingerafdruk(nieuw_kopia)}")
        self.schrijf(
            rotatietaak=task_id,
            nieuwe_public_key=nieuw_pub,
            nieuw_kopia=nieuw_kopia,
            rotatie_resultaat=res,
            nieuwe_age_key_fp=vingerafdruk(nieuwe_age),
        )

    def _uit_opi(self, snippet: str, aantal: int) -> list[str]:
        """Laat de OPI-pod waarden uitrekenen met zijn eigen code en lees ze terug.

        Met een merkteken per regel: het importeren van `opi` logt zelf naar de uitvoer, dus
        de laatste regels zijn niet de antwoorden.
        """
        code, uit = self.in_opi(f'python -c "{snippet}"')
        waarden = [r.split("MEET:", 1)[1].strip() for r in uit.splitlines() if "MEET:" in r]
        if len(waarden) != aantal:
            raise RuntimeError(f"verwachtte {aantal} waarden uit de OPI-pod, kreeg {len(waarden)}: {uit[-400:]}")
        return waarden

    def _bucket_en_prefix(self, project: str, namespace: str) -> tuple[str, str]:
        """De bucket en de prefix van de Kopia-repository, uit OPI's eigen naamgeving."""
        bucket, prefix = self._uit_opi(
            "from opi.manager.backup.base import get_backup_bucket_name; "
            "from opi.utils.naming import generate_backup_prefix; "
            f"print('MEET:' + get_backup_bucket_name('{project}', '{self.cluster}')); "
            f"print('MEET:' + generate_backup_prefix('{self.cluster}', '{namespace}'))",
            2,
        )
        return bucket, prefix

    def _bucket_account(self, bucket: str) -> tuple[str, str]:
        """Het S3-account dat een backup-pod voor deze bucket meekrijgt, uit OPI's eigen code.

        Niet nagebouwd: de naam en de afleiding komen uit `opi.manager.backup.base`, en de
        beheersleutel uit de settings van de pod zelf.
        """
        naam, geheim = self._uit_opi(
            "from opi.core.config import settings; "
            "from opi.manager.backup.base import backup_bucket_account_name, derive_bucket_account_secret; "
            f"b='{bucket}'; print('MEET:' + backup_bucket_account_name(b)); "
            "print('MEET:' + derive_bucket_account_secret(settings.BACKUP_S3_SECRET_KEY, b))",
            2,
        )
        return naam, geheim

    def _kopia_probe(self, bucket: str, prefix: str, wachtwoord: str, extra: str = "") -> tuple[int, str]:
        ak, sk = self._bucket_account(bucket)
        script = (
            f"export KOPIA_PASSWORD='{wachtwoord}' AWS_ACCESS_KEY_ID='{ak}' AWS_SECRET_ACCESS_KEY='{sk}'; "
            "rm -rf /tmp/meet289 && mkdir -p /tmp/meet289 && "
            f"kopia repository connect s3 --bucket {bucket} --prefix {prefix}/ --endpoint {self.s3_endpoint} "
            f"--disable-tls --cache-directory /tmp/meet289/cache --config-file /tmp/meet289/repo.config "
            f"--no-check-for-updates 2>&1 | tail -5" + (f" && {extra}" if extra else "")
        )
        return self.in_opi(script, check=False)

    def probe(self) -> None:
        s = self.lees()
        bucket, prefix = self._bucket_en_prefix(s["project"], s["namespace"])
        log(f"kopia-probe op bucket={bucket} prefix={prefix}")
        code_oud, uit_oud = self._kopia_probe(bucket, prefix, s["oud_kopia"])
        log(f"  met het OUDE afgeleide wachtwoord: exit={code_oud}")
        for regel in uit_oud.strip().splitlines()[-3:]:
            log(f"    | {regel}")
        code_nieuw, uit_nieuw = self._kopia_probe(
            bucket,
            prefix,
            s["nieuw_kopia"],
            extra="kopia snapshot list --config-file /tmp/meet289/repo.config --json | "
            "python -c 'import json,sys; print(\"snapshots:\", len(json.load(sys.stdin)))'",
        )
        log(f"  met het NIEUWE afgeleide wachtwoord: exit={code_nieuw}")
        for regel in uit_nieuw.strip().splitlines()[-3:]:
            log(f"    | {regel}")
        self.schrijf(
            probe_oud_exit=code_oud,
            probe_oud=uit_oud.strip().splitlines()[-2:],
            probe_nieuw_exit=code_nieuw,
            probe_nieuw=uit_nieuw.strip().splitlines()[-2:],
            bucket=bucket,
            prefix=prefix,
        )

    def vul2(self) -> None:
        s = self.lees()
        ns = s["namespace"]
        db = self.geheim(ns, f"{s['deployment']}-database")
        code, uit = self.psql(db, f'INSERT INTO "{s["schema"]}".voor VALUES (4),(5);')  # noqa: S608
        log(f"twee rijen erbij: {code} {uit.splitlines()[:1]}")
        if code != 0:
            raise RuntimeError(uit[:800])
        pvc = s["pvcs"][0]
        self.in_pvc(ns, pvc, "echo na-rotatie > /mnt/rc289.txt && cat /mnt/rc289.txt")
        log(f"PVC {pvc} bijgewerkt: rc289.txt = na-rotatie")
        self.schrijf(rijen_na=5, bestand_na="na-rotatie", db_na_rotatie=db["DATABASE_DB"])

    def restore(self, label: str) -> None:
        s = self.lees()
        p, d, key = s["project"], s["deployment"], s["api_key"]
        run_id = s[f"run_{label}"]
        log(f"restore van run {run_id} ({label})")
        r = self.api("POST", f"/api/v1/restore/project/{p}/deployment/{d}/run/{run_id}", key, json={})
        log(f"  POST restore -> {r.status_code}")
        if r.status_code != 200:
            raise RuntimeError(r.text[:1500])
        body = r.json()
        log(f"  status={body.get('status')} refresh={body.get('refresh_succeeded')}")
        for det in body.get("pvcs_restored", []):
            log(
                f"  teruggezet: {det.get('pvc_name')} -> {det.get('target_pvc_name')} "
                f"ok={det.get('success')} {det.get('error') or ''}"
            )
        self.schrijf(
            **{
                f"restore_{label}": {
                    "status": body.get("status"),
                    "refresh": body.get("refresh_succeeded"),
                    "pvcs": [
                        {k: det.get(k) for k in ("pvc_name", "target_pvc_name", "success", "error")}
                        for det in body.get("pvcs_restored", [])
                    ],
                }
            }
        )

    def controleer(self, label: str, rijen: int, inhoud: str) -> None:
        """Lees na een restore de teruggezette database en de teruggezette PVC."""
        s = self.lees()
        ns = s["namespace"]
        details = [d for d in s[f"restore_{label}"]["pvcs"] if d.get("success")]
        # Welk doel een PVC is en welk een database staat niet in het antwoord: het cluster weet het.
        pvc_doel = next((d["target_pvc_name"] for d in details if self.bestaat_pvc(ns, d["target_pvc_name"])), None)
        db_doel = next((d["target_pvc_name"] for d in details if d["target_pvc_name"] != pvc_doel), None)

        db = self.geheim(ns, f"{s['deployment']}-database")
        # Het schema heet in de teruggezette database anders; twee pogingen voor een traag startende pod.
        schemas: list[str] = []
        for _poging in (1, 2):
            _, uit_schema = self.psql(
                db,
                "SELECT 'MEET:' || table_schema FROM information_schema.tables WHERE table_name = 'voor'",
                database=db_doel,
            )
            schemas = [r.split("MEET:", 1)[1].strip() for r in uit_schema.splitlines() if "MEET:" in r]
            if schemas:
                break
        gemeten = "geen tabel 'voor' gevonden"
        if schemas:
            _, uit = self.psql(db, f"SELECT 'MEET:' || count(*) FROM \"{schemas[0]}\".voor", database=db_doel)  # noqa: S608
            treffers = [r.split("MEET:", 1)[1].strip() for r in uit.splitlines() if "MEET:" in r]
            gemeten = treffers[0] if treffers else uit[:200]
        log(f"  rijen in {db_doel} (schema {schemas[:1]}): {gemeten} (verwacht {rijen})")

        inhoud_gemeten = ""
        if pvc_doel:
            uitvoer = self.in_pvc(ns, pvc_doel, "cat /mnt/rc289.txt")
            inhoud_gemeten = uitvoer.strip().splitlines()[0] if uitvoer.strip() else ""
            log(f"  inhoud van {pvc_doel}: {inhoud_gemeten!r} (verwacht {inhoud!r})")
        self.schrijf(
            **{
                f"controle_{label}": {
                    "database": db_doel,
                    "rijen": gemeten,
                    "rijen_verwacht": rijen,
                    "pvc": pvc_doel,
                    "inhoud": inhoud_gemeten,
                    "inhoud_verwacht": inhoud,
                }
            }
        )

    def _sops_paden(self, project: str) -> list[str]:
        """De paden van elk ``*.sops.yaml`` van dit project in zad-deployments."""
        with httpx.Client(verify=False, timeout=60.0, auth=(self.forgejo_user, self.forgejo_password)) as c:  # noqa: S501 - zelfgetekend sandboxcertificaat
            r = c.get(
                f"{self.forgejo_url}/api/v1/repos/rig-admin/zad-deployments/git/trees/HEAD",
                params={"recursive": "true", "per_page": "1000"},
            )
        if r.status_code != 200:
            raise RuntimeError(f"boom van zad-deployments niet te lezen: {r.status_code} {r.text[:200]}")
        prefix = f"{self.cluster}/{project}/"
        return sorted(
            item["path"]
            for item in r.json().get("tree", [])
            if item.get("path", "").startswith(prefix) and item["path"].endswith(".sops.yaml")
        )

    def recipients(self) -> None:
        """De recipients van elk SOPS-bestand van het project, en de staat van zijn Applications.

        De recipient staat als klare tekst in de metadata van het bestand zelf, dus dit leest
        de repo en niet `sops filestatus`, waarvoor een sleutel nodig is.
        """
        s = self.lees()
        p = s["project"]
        oud, nieuw = s.get("oude_public_key", ""), s.get("nieuwe_public_key", "")
        paden = self._sops_paden(p)
        log(f"{len(paden)} SOPS-bestanden van {p} in zad-deployments")
        bestanden = []
        for pad in paden:
            metadata = yaml.safe_load(self._uit_zad_deployments(pad)).get("sops", {})
            gevonden = [item.get("recipient", "") for item in metadata.get("age", [])]
            stand = "nieuw" if gevonden == [nieuw] else ("oud" if oud in gevonden else "anders")
            log(f"  {pad}: {stand} ({len(gevonden)} recipient(s))")
            bestanden.append({"pad": pad, "recipients": gevonden, "stand": stand})

        # -A en niet -n argocd: op deze sandbox staan de Applications in rig-system.
        apps = json.loads(self.kubectl("get", "applications", "-A", "-o", "json"))
        standen = []
        for app in apps.get("items", []):
            naam = app["metadata"]["name"]
            if not (naam == p or naam.startswith(f"{p}-")):
                continue
            status = app.get("status", {})
            stand = {
                "app": naam,
                "sync": status.get("sync", {}).get("status"),
                "health": status.get("health", {}).get("status"),
                "revision": (status.get("sync", {}).get("revision") or "")[:12],
            }
            log(f"  Application {naam}: sync={stand['sync']} health={stand['health']} rev={stand['revision']}")
            standen.append(stand)
        if not standen:
            log(f"  geen Application met naam {p} of {p}-* gevonden")
        self.schrijf(sops_bestanden=bestanden, applications=standen)

    def uitslagen(self) -> None:
        """De einduitslag van de aanmaaktaak en de rotatietaak, voor zover ze al klaar zijn.

        Apart van de fasen die ze starten: de rondgang hoeft niet op het ArgoCD-budget van
        600s te wachten om te kunnen meten, maar de uitslag hoort wel in het verslag.
        """
        s = self.lees()
        if s.get("aanmaaktaak"):
            try:
                status, naam = self.wacht_op_aanmaak(s["aanmaaktaak"], timeout=30.0)
            except TimeoutError:
                status = "loopt nog"
            log(f"aanmaaktaak {s['aanmaaktaak']}: {status}")
            self.schrijf(aanmaakstatus=status)
        if s.get("rotatietaak"):
            try:
                body = self.wacht_op_taak(s["rotatietaak"], s["api_key"], timeout=30.0)
            except TimeoutError:
                self.schrijf(rotatiestatus="loopt nog")
                log(f"rotatietaak {s['rotatietaak']}: loopt nog")
                return
            res = {k: v for k, v in (body.get("result") or {}).items() if k != "processing"}
            log(f"rotatietaak {s['rotatietaak']}: {body.get('status')} {json.dumps(res)[:1200]}")
            self.schrijf(rotatiestatus=body.get("status"), rotatie_resultaat=res)

    def op(self) -> None:
        s = self.lees()
        r = self.api(
            "DELETE", f"/api/projects/{s['project']}", s["api_key"], json={"confirmDeletion": True, "force": True}
        )
        log(f"DELETE /api/projects/{s['project']} -> {r.status_code} {r.text[:200]}")
        self.schrijf(opgeruimd=r.status_code)


_ALLES = (
    "maak",
    "sync",
    "vul",
    "backup1",
    "oude-sleutel",
    "roteer",
    "probe",
    "vul2",
    "backup2",
    "restore1",
    "controleer1",
    "restore2",
    "controleer2",
    "recipients",
    "uitslagen",
    "op",
)


def _fasen(meting: Meting) -> dict[str, Any]:
    return {
        "maak": meting.maak,
        "sync": meting.sync,
        "vul": meting.vul,
        "backup1": lambda: meting.backup("voor"),
        "oude-sleutel": meting.oude_sleutel,
        "roteer": meting.roteer,
        "probe": meting.probe,
        "vul2": meting.vul2,
        "backup2": lambda: meting.backup("na"),
        "restore1": lambda: meting.restore("voor"),
        "controleer1": lambda: meting.controleer("voor", 3, "voor-rotatie"),
        "restore2": lambda: meting.restore("na"),
        "controleer2": lambda: meting.controleer("na", 5, "na-rotatie"),
        "recipients": meting.recipients,
        "uitslagen": meting.uitslagen,
        "op": meting.op,
    }


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("fasen", nargs="+", help=f"'alles' of een of meer van: {', '.join(_ALLES)}")
    p.add_argument("--base-url", default=os.environ.get("E2E_BASE_URL", "https://zad.sandbox.rijksapp.dev"))
    p.add_argument(
        "--secret-key",
        default=os.environ.get("E2E_SECRET_KEY", ""),
        help="SECRET_KEY van het cluster, waarmee de sessiecookie wordt ondertekend",
    )
    p.add_argument("--zad-token", default=os.environ.get("ZAD_TOKEN", ""))
    p.add_argument(
        "--zad-otp",
        default=os.environ.get("ZAD_OTP", ""),
        help="verse code van de beheerder, die de rotatieroute naast het token vraagt",
    )
    p.add_argument("--email", default=os.environ.get("MEET_EMAIL", "admin@sandbox.rijksapp.dev"))
    p.add_argument("--cluster", default=os.environ.get("MEET_CLUSTER", "sandboxed-local"))
    p.add_argument("--context", default=os.environ.get("MEET_CONTEXT", "kind-rig-sandbox"))
    p.add_argument("--opi-namespace", default=os.environ.get("MEET_OPI_NAMESPACE", "rig-system"))
    p.add_argument("--image", default=os.environ.get("MEET_IMAGE", "ghcr.io/minbzk/base-images/e2e-allservices:latest"))
    p.add_argument("--s3-endpoint", default=os.environ.get("MEET_S3_ENDPOINT", "minio.rig-backup-destination.svc:9000"))
    p.add_argument("--toestand", default=os.environ.get("MEET_TOESTAND", "/tmp/rc289-meting.json"))
    p.add_argument("--forgejo-url", default=os.environ.get("FORGEJO_URL", "https://forgejo.sandbox.rijksapp.dev"))
    p.add_argument("--forgejo-user", default=os.environ.get("FORGEJO_USER", "rig-admin"))
    p.add_argument("--forgejo-password", default=os.environ.get("FORGEJO_PASSWORD", ""))
    args = p.parse_args(argv)

    if not args.secret_key or not args.zad_token:
        p.error("E2E_SECRET_KEY en ZAD_TOKEN zijn nodig; zie de docstring van dit script")

    meting = Meting(args)
    fasen = _fasen(meting)
    gevraagd = _ALLES if args.fasen == ["alles"] else args.fasen
    for naam in gevraagd:
        if naam not in fasen:
            p.error(f"onbekende fase {naam!r}; keuze: {', '.join(_ALLES)}")
    for naam in gevraagd:
        log(f"=== fase {naam}")
        fasen[naam]()
    log("klaar")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
