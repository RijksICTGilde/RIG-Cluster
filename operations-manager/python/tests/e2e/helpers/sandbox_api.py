"""
Sandbox API helpers: geef een project een API-token en drijf de REST-endpoints aan.

De endpoints hieronder legitimeren zich met ``X-API-Key``. Sinds schemaversie 2.12 is dat
geen ``config.api-key`` meer die van de detailpagina te lezen is, maar een TOKEN van het
service account ``project-api``, waarvan alleen de hash in het projectbestand staat. Een
bestaand token is dus niet op te halen: de weg is er een UITGEVEN, en dat doet
:func:`geef_projecttoken` over het tabblad Team met de sessiecookie van de e2e-gebruiker.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import time
from datetime import UTC, datetime
from urllib.parse import urlparse

import httpx
from itsdangerous import TimestampSigner
from tests.e2e.helpers import cluster

logger = logging.getLogger(__name__)


#: De namen uit het portaal die de uitgifte hieronder nodig heeft: het service account dat
#: de API-toegang van een project draagt (``opi/services/schema_migration.py``) en de
#: CSRF-cookie en -header (``opi/utils/csrf.py``). Letterlijk en niet geimporteerd, want dit
#: bestand moet importeerbaar blijven ZONDER de OPI-config: ``scripts/sandbox_project_tool.py``
#: praat tegen een sandbox op afstand en heeft geen platformsleutel, en een import van
#: ``opi.core.config`` beeindigt daar het proces. Dat ze gelijk blijven staat in
#: ``tests/test_e2e_helpers.py``.
PROJECT_API_ACCOUNT = "project-api"
CSRF_COOKIE_NAME = "csrf_token"
CSRF_HEADER_NAME = "x-csrf-token"

#: De sessiesleutel met het moment van de laatste step-up (``opi/core/step_up.py``). Een
#: token uitgeven vraagt een verse (RC-387), en wie zijn eigen sessie ondertekent zet hem
#: er dus bij: zonder stempel antwoordt het portaal met een 403 in plaats van een token.
STEP_UP_SESSION_KEY = "step_up_at"

#: The human admin the e2e suite creates its projects as (``SANDBOX_TEST_USER`` in the
#: e2e conftest, which this module cannot import without the OPI config).
SANDBOX_ADMIN_EMAIL = os.environ.get("ZAD_USER_EMAIL", "admin@sandbox.rijksapp.dev")

#: Hoe een uitgegeven token eruitziet: het voorvoegsel plus de willekeurige waarde.
_TOKEN_VORM = re.compile(r'value="(zad_[A-Za-z0-9_-]+)"')


def make_session_cookie(data: dict, *, secret: str, max_age_seconds: int = 28800) -> str:
    """Maak een server-side sessie op de SANDBOX en geef de ondertekende cookie terug.

    Sinds RC-390 draagt de cookie alleen een ondertekend id; de inhoud woont in de tabel
    ``sessions`` van de rig-db. Deze helper schrijft die rij (via een psql-pod, zoals
    ``cluster.run_psql`` dat doet) en tekent het id met de SECRET_KEY van het cluster.

    Een ``user`` in ``data`` krijgt een vers ``login_at``: de guard weigert een sessie
    zonder inlogtijdstip (opi/core/session_guard.py).
    """
    if gebruiker := data.get("user"):
        data = {**data, "user": {**gebruiker, "login_at": time.time()}}
    session_id = secrets.token_urlsafe(32)
    # SQL-literal-escaping: de data gaat als een letterlijke string de psql-pod in.
    data_sql = json.dumps(data).replace("'", "''")
    password = cluster.secret_values("rig-system", "postgres-admin-credentials")["password"]
    code, uitvoer = cluster.run_psql(
        "INSERT INTO sessions (id, data, created_at, expires_at, last_seen) "
        f"VALUES ('{session_id}', '{data_sql}'::jsonb, now(), "
        f"now() + interval '{max_age_seconds} seconds', now()) "
        "ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data, expires_at = EXCLUDED.expires_at, "
        "last_seen = now()",
        host="rig-db-rw",
        user="postgres",
        database="operations_manager",
        password=password,
    )
    if code != 0:
        raise AssertionError(f"sessie-rij aanmaken op de sandbox mislukte (exit {code}): {uitvoer[:300]}")
    return TimestampSigner(secret).sign(session_id.encode()).decode()


def geef_projecttoken(
    base_url: str,
    project_name: str,
    *,
    sessie_cookie: str,
    lid: str = PROJECT_API_ACCOUNT,
    tokennaam: str | None = None,
    dagen: int = 1,
    verify_ssl: bool = True,
    timeout: float = 60.0,
) -> str:
    """Geef een nieuw API-token uit voor ``lid`` en geef de waarde terug.

    Zonder browser: het tabblad Team is een formulier, dus een sessiecookie, de
    ``csrf_token``-cookie terug in de header en een ``Origin`` op dezelfde host zijn alles
    wat het nodig heeft (dezelfde drie eisen als ``weesprojecten.start_verwijdering``).

    De uitgegeven waarde staat EEN keer op de pagina waar de 303 naartoe gaat, dus hij
    komt hier uit het antwoord op de POST nadat de omleiding gevolgd is. Wie hem kwijt is
    geeft een nieuwe uit; teruglezen kan niemand, ook de uitgever niet.

    De sessiecookie moet een verse step-up dragen (:data:`STEP_UP_SESSION_KEY`): uitgeven
    vraagt er een, en zonder stempel antwoordt het portaal met een 403.
    """
    basis = base_url.rstrip("/")
    host = urlparse(basis).hostname or ""
    # Met een willekeurig staartje: twee pogingen binnen dezelfde seconde zouden op
    # dezelfde naam botsen, en dan weigert de dienst de tweede.
    tokennaam = tokennaam or f"e2e-{int(time.time())}-{secrets.token_hex(3)}"
    # De client volgt GEEN omleidingen: een sessie die niet aanvaard wordt loopt naar de
    # inlogpagina van Keycloak, en die antwoordt zelf 200. Een gevolgde omleiding zou hier
    # dus als een geslaagd tabblad lezen. De POST volgt zijn 303 wel, want daar staat de
    # waarde.
    with httpx.Client(verify=verify_ssl, timeout=timeout, follow_redirects=False) as client:
        client.cookies.set("session", sessie_cookie, domain=host)
        antwoord = client.get(f"{basis}/projects/{project_name}/team")
        if antwoord.status_code != 200:
            raise AssertionError(
                f"het tabblad Team van '{project_name}' gaf HTTP {antwoord.status_code} "
                f"({antwoord.headers.get('location', 'geen omleiding')}); klopt de "
                f"sessiecookie met de SECRET_KEY van het cluster?"
            )
        csrf = client.cookies.get(CSRF_COOKIE_NAME)
        if not csrf:
            raise AssertionError(f"geen {CSRF_COOKIE_NAME}-cookie na GET van het tabblad Team van '{project_name}'")
        if lid == PROJECT_API_ACCOUNT:
            # Een project dat na RC-322 nieuw is, heeft dit service account niet: alleen de
            # migratie van 2.12 maakte het voor een bestaande projectsleutel. Bestaat het al,
            # dan weigert de dienst het toevoegen en loopt dit gewoon door naar het uitgeven.
            client.post(
                f"{basis}/projects/{project_name}/leden/toevoegen",
                data={"naam": lid, "rol": "admin"},
                headers={CSRF_HEADER_NAME: csrf, "Origin": basis},
            )
        antwoord = client.post(
            f"{basis}/projects/{project_name}/leden/{lid}/tokens",
            data={"tokennaam": tokennaam, "dagen": str(dagen)},
            headers={CSRF_HEADER_NAME: csrf, "Origin": basis},
            follow_redirects=True,
        )
    if antwoord.status_code != 200:
        raise AssertionError(
            f"het uitgeven van een token voor '{lid}' op '{project_name}' gaf HTTP "
            f"{antwoord.status_code}: {antwoord.text[:300]}"
        )
    treffer = _TOKEN_VORM.search(antwoord.text)
    if not treffer:
        # De pagina draagt in dit geval de weigering van de dienst in een alert.
        raise AssertionError(
            f"geen uitgegeven token op het tabblad Team van '{project_name}' na het aanmaken; "
            f"de pagina zei: {_melding(antwoord.text)}"
        )
    return treffer.group(1)


def _melding(html: str) -> str:
    """De weigering die de pagina draagt, voor een leesbare mislukking."""
    treffer = re.search(r'type="error" heading="([^"]+)">\s*<c-paragraph>([^<]*)', html)
    if treffer:
        return f"{treffer.group(1)}: {treffer.group(2).strip()}"
    kop = re.findall(r'heading="([^"]+)"', html)
    return kop[-1] if kop else "niets bruikbaars"


def start_task(
    base_url: str,
    method: str,
    path: str,
    api_key: str,
    body: dict,
    *,
    verify_ssl: bool = True,
) -> str:
    """Fire an async v1/v2 API request and return its task id WITHOUT waiting.

    Used by the real-life suite to start mutations on several projects at once;
    the server processes the tasks concurrently while the test continues (e.g.
    driving UI edits on other projects). Pair with wait_for_task().
    """
    base = base_url.rstrip("/")
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
    with httpx.Client(verify=verify_ssl, timeout=30.0) as client:
        response = client.request(method, f"{base}{path}", json=body, headers=headers)
        assert response.status_code == 202, (
            f"Expected 202 from {method} {path}, got {response.status_code}: {response.text}"
        )
        location = response.headers.get("Location")
        task_id = location.rsplit("/", 1)[-1] if location else response.json().get("task_id")
        assert task_id, f"No task id returned from {method} {path}: {response.text}"
        return task_id


def wait_for_task(
    base_url: str,
    task_id: str,
    api_key: str,
    *,
    verify_ssl: bool = True,
    timeout: float = 180.0,
) -> dict:
    """Poll a task started with start_task() until it completes successfully."""
    base = base_url.rstrip("/")
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
    with httpx.Client(verify=verify_ssl, timeout=30.0) as client:
        return _wait_for_task(client, base, task_id, headers, timeout=timeout)


def task_outcome(
    base_url: str,
    task_id: str,
    api_key: str,
    *,
    verify_ssl: bool = True,
    timeout: float = 180.0,
) -> tuple[str, str | None]:
    """Poll a task and report its outcome without asserting.

    Returns ("completed", None), ("superseded", None), ("failed", reason) or
    ("running", None) when the task has not reached a terminal state before the
    timeout. "superseded" is a benign outcome: a newer task whose scope covers
    this one took over, so this task's ArgoCD wait was abandoned by design. It is
    recorded as a completed task carrying result.status == "superseded".
    """
    base = base_url.rstrip("/")
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
    deadline = time.monotonic() + timeout
    with httpx.Client(verify=verify_ssl, timeout=30.0) as client:
        while time.monotonic() < deadline:
            response = client.get(f"{base}/api/tasks/{task_id}", headers=headers)
            if response.status_code == 200:
                task = response.json()
                status = task.get("status")
                if status != "completed":
                    return "failed", f"task status '{status}': {task}"
                if (task.get("result") or {}).get("status") == "superseded":
                    return "superseded", None
                try:
                    _assert_no_subtask_failure(task_id, task)
                except AssertionError as exc:
                    return "failed", str(exc)
                return "completed", None
            if response.status_code != 202:
                return "failed", f"unexpected task poll status {response.status_code}: {response.text}"
            time.sleep(3.0)
    return "running", None


def add_component(
    base_url: str,
    project_name: str,
    api_key: str,
    *,
    component_name: str,
    image: str,
    deployment_names: list[str],
    verify_ssl: bool = True,
    timeout: float = 180.0,
) -> dict:
    """Add one component via the v2 async API and wait for the task to finish.

    Returns the terminal task response dict. Raises AssertionError if the task
    does not reach 'completed'.
    """
    base = base_url.rstrip("/")
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
    body = {
        "name": component_name,
        "image": image,
        "deployment_names": deployment_names,
    }
    with httpx.Client(verify=verify_ssl, timeout=30.0) as client:
        response = client.post(
            f"{base}/api/v2/projects/{project_name}/components",
            json=body,
            headers=headers,
        )
        assert response.status_code == 202, (
            f"Expected 202 from component-add, got {response.status_code}: {response.text}"
        )
        location = response.headers.get("Location")
        task_id = location.rsplit("/", 1)[-1] if location else response.json().get("task_id")
        assert task_id, f"No task id returned from component-add: {response.text}"

        return _wait_for_task(client, base, task_id, headers, timeout=timeout)


def delete_component(
    base_url: str,
    project_name: str,
    api_key: str,
    *,
    component_name: str,
    confirm_in_use: bool = False,
    verify_ssl: bool = True,
    timeout: float = 180.0,
) -> tuple[int, dict]:
    """Delete one component via the v2 async API.

    Returns (status_code, body). On 202 the task is waited out and the terminal task
    response is the body; on any other status the response body is returned unchanged --
    a 409 (still in use) and a 404 (no such component) are answers this endpoint gives on
    purpose, so the caller decides what they mean rather than this helper asserting.
    """
    base = base_url.rstrip("/")
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
    params = {"confirm_in_use": "true"} if confirm_in_use else None
    with httpx.Client(verify=verify_ssl, timeout=30.0) as client:
        response = client.delete(
            f"{base}/api/v2/projects/{project_name}/components/{component_name}",
            params=params,
            headers=headers,
        )
        if response.status_code != 202:
            return response.status_code, response.json()

        location = response.headers.get("Location")
        task_id = location.rsplit("/", 1)[-1] if location else response.json().get("task_id")
        assert task_id, f"No task id returned from component-delete: {response.text}"
        return 202, _wait_for_task(client, base, task_id, headers, timeout=timeout)


def _admin_session(gebruiker: dict, secret_key: str) -> str:
    """A signed session for ``gebruiker`` with a fresh step-up.

    Deleting a project asks for one (RC-332); without the stamp the portal answers with
    the OTP dialog instead of starting the task.
    """
    return make_session_cookie(
        {"user": gebruiker, STEP_UP_SESSION_KEY: datetime.now(UTC).isoformat()}, secret=secret_key
    )


def start_verwijdering(
    base_url: str,
    naam: str,
    *,
    secret_key: str,
    gebruiker: dict,
    verify_ssl: bool = False,
    timeout: float = 60.0,
) -> str:
    """Dien de verwijdering van ``naam`` in over de webroute, als de e2e-sandboxgebruiker.

    De route maakt er een taak van en antwoordt met het voortgangsfragment, dus een 200
    zegt dat de verwijdering is AANGENOMEN en niet dat ze klaar is. Geeft het pad terug
    waarop dat fragment zichzelf ververst, zodat de aanroeper de TAAK kan volgen in plaats
    van de klok; leeg als het antwoord er geen draagt.

    Gooit een ``RuntimeError`` als de route het verzoek niet aanneemt, met wat de server
    terugzei: de drie eisen hierboven falen allemaal als een 403 met een eigen melding.
    """
    basis = base_url.rstrip("/")
    host = urlparse(basis).hostname or ""
    sessie = _admin_session(gebruiker, secret_key)
    with httpx.Client(verify=verify_ssl, timeout=timeout, follow_redirects=False) as client:
        client.cookies.set("session", sessie, domain=host)
        # Een GET zet de csrf_token-cookie (de middleware munt hem als hij ontbreekt) en
        # laat meteen zien of de sessiecookie wordt aanvaard: zonder geldige sessie komt
        # hier een omleiding naar de login en geen 200.
        antwoord = client.get(f"{basis}/projects")
        if antwoord.status_code != 200:
            raise RuntimeError(
                f"de sessie wordt niet aanvaard op {basis}: GET /projects gaf HTTP "
                f"{antwoord.status_code}. Klopt E2E_SECRET_KEY met de SECRET_KEY van het cluster?"
            )
        csrf = client.cookies.get(CSRF_COOKIE_NAME)
        if not csrf:
            raise RuntimeError(f"geen {CSRF_COOKIE_NAME}-cookie na GET /projects op {basis}")

        antwoord = client.post(
            f"{basis}/projects/delete/{naam}",
            headers={CSRF_HEADER_NAME: csrf, "Origin": basis},
        )
    # The step-up dialog answers 200 too; it is a refusal, not an accepted deletion.
    if antwoord.status_code != 200 or "data-step-up" in antwoord.text:
        raise RuntimeError(
            f"het verwijderen van '{naam}' werd niet aangenomen (HTTP {antwoord.status_code}): {_leesbaar(antwoord.text)}"
        )
    treffer = re.search(r'hx-get="([^"]+)"', antwoord.text)
    return treffer.group(1) if treffer else ""


def wacht_tot_de_taak_klaar_is(
    base_url: str,
    poll_pad: str,
    *,
    secret_key: str,
    gebruiker: dict,
    verify_ssl: bool = False,
    timeout: float = 600.0,
    interval: float = 3.0,
) -> str:
    """Volg het voortgangsfragment tot de taak klaar is en geef zijn laatste tekst terug.

    Het fragment ververst zichzelf alleen zolang de taak loopt (``hx-get`` staat er bij
    status "completed" en "failed" niet meer in), dus het ONTBREKEN van die poll is de
    eindtoestand. Zo komt een weigering van OPI eruit als zijn eigen melding, en niet als
    een wachttijd die afliep: zonder dit duurde een mislukte verwijdering 600 seconden om
    "staat er nog" te zeggen, terwijl OPI na 18 seconden al had opgeschreven waarom.
    """
    basis = base_url.rstrip("/")
    host = urlparse(basis).hostname or ""
    sessie = _admin_session(gebruiker, secret_key)
    einde = time.monotonic() + timeout
    tekst = ""
    with httpx.Client(verify=verify_ssl, timeout=60.0, follow_redirects=False) as client:
        client.cookies.set("session", sessie, domain=host)
        while time.monotonic() < einde:
            antwoord = client.get(f"{basis}{poll_pad}")
            # The fragment lives under the project, so a project that is gone answers 404:
            # that is the deletion finishing, not a failure to read it.
            if antwoord.status_code == 404 and tekst:
                return "klaar: het project bestaat niet meer"
            if antwoord.status_code != 200:
                return f"de voortgang is niet te lezen (HTTP {antwoord.status_code})"
            tekst = antwoord.text
            if "hx-get" not in tekst:
                return _leesbaar(tekst)
            time.sleep(interval)
    return f"de taak was na {timeout:.0f}s nog niet klaar: {_leesbaar(tekst)}"


def _leesbaar(html: str) -> str:
    """De tekst uit een fragment, zonder tags en zonder lege regels."""
    zonder_tags = re.sub(r"<[^>]+>", " ", html)
    return " ".join(zonder_tags.split())[:400]


def delete_project_via_api(
    base_url: str,
    project_name: str,
    api_key: str,
    *,
    force: bool = True,
    verify_ssl: bool = True,
    timeout: float = 180.0,
) -> None:
    """Safety-net deletion of a test project, through the portal as the sandbox admin.

    Not through ``DELETE /api/projects/{name}`` with the project's token any more: since
    RC-392 the roles of ``project-api`` do not open ``project.delete``, so every teardown
    got a 403 and left its project on the sandbox. ``api_key`` and ``force`` are kept so
    the callers stay as they are; the portal deletes the way a person would.

    Best-effort: a refusal or a task that does not finish is logged, never raised, so it
    never breaks fixture teardown. The cluster is only force-cleaned when OPI accepted
    the deletion: tearing a project out from under OPI without that leaves it gone from
    the cluster and still present in ZAD, a half-state the next run inherits.
    """
    del api_key, force
    secret_key = os.environ.get("E2E_SECRET_KEY") or os.environ.get("ZAD_SECRET_KEY", "")
    admin = {"sub": "sandbox-e2e-user", "email": SANDBOX_ADMIN_EMAIL, "name": "Sandbox E2E User"}
    try:
        poll_path = start_verwijdering(
            base_url, project_name, secret_key=secret_key, gebruiker=admin, verify_ssl=verify_ssl
        )
    except (RuntimeError, httpx.HTTPError) as refused:
        logger.warning("Delete of '%s' was not accepted; it stays on the sandbox: %s", project_name, refused)
        return
    if poll_path:
        outcome = wacht_tot_de_taak_klaar_is(
            base_url, poll_path, secret_key=secret_key, gebruiker=admin, verify_ssl=verify_ssl, timeout=timeout
        )
        logger.info("Delete of '%s': %s", project_name, outcome)

    # Belt-and-suspenders: the project delete only drops the namespace once the
    # ArgoCD app deletion is confirmed, which frequently times out on a busy
    # sandbox and leaves an empty namespace + dangling app that pile up across
    # runs. Reclaim them directly so test projects never accumulate.
    cluster.force_cleanup_project(project_name)


def _wait_for_task(
    client: httpx.Client,
    base: str,
    task_id: str,
    headers: dict,
    *,
    timeout: float,
    interval: float = 3.0,
) -> dict:
    """Poll GET /api/tasks/{task_id} until terminal (200), then assert real success.

    The task envelope reports status 'completed' even when the underlying operation
    failed - the actual outcome is in `result.status` and in the subtasks. We inspect
    both so a failed operation surfaces its real error instead of a vague downstream
    assertion.
    """
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        resp = client.get(f"{base}/api/tasks/{task_id}", headers=headers)
        # 202 = still running, 200 = terminal (completed/failed/cancelled)
        if resp.status_code == 200:
            last = resp.json()
            status = last.get("status")
            assert status == "completed", f"Task {task_id} ended with status '{status}': {last}"
            _assert_no_subtask_failure(task_id, last)
            return last
        if resp.status_code not in (200, 202):
            raise AssertionError(f"Unexpected task poll status {resp.status_code}: {resp.text}")
        last = resp.json()
        time.sleep(interval)
    raise AssertionError(f"Task {task_id} did not complete within {timeout}s (last: {last})")


def _assert_no_subtask_failure(task_id: str, task: dict) -> None:
    """Raise with the real error if the task's result or any subtask failed."""
    result = task.get("result") or {}
    if result.get("status") == "failed":
        error = task.get("error_message") or result.get("error") or "unknown error"
        raise AssertionError(f"Task {task_id} operation failed: {error} (result={result})")
    for sub in task.get("subtasks") or []:
        if sub.get("status") == "failed":
            raise AssertionError(
                f"Task {task_id} subtask '{sub.get('name')}' failed: {sub.get('error') or 'unknown error'}"
            )
